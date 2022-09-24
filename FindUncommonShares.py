#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File name          : FindUncommonShares.py
# Author             : Podalirius (@podalirius_)
# Date created       : 30 Jan 2022


from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from impacket import version
from impacket.smbconnection import SMBConnection, SMB2_DIALECT_002, SMB2_DIALECT_21, SMB_DIALECT, SessionError
from impacket.spnego import SPNEGO_NegTokenInit, TypesMech
import argparse
import binascii
import dns.resolver, dns.exception
import json
import ldap3
import logging
import nslookup
import os
import sqlite3
import ssl
import sys
import threading
import time
import traceback
import xlsxwriter


VERSION = "2.3"


COMMON_SHARES = [
    "C$",
    "ADMIN$", "IPC$",
    "PRINT$", "print$",
    "fax$", "FAX$",
    "SYSVOL", "NETLOGON"
]


def STYPE_MASK(stype_value):
    known_flags = {
        ## One of the following values may be specified. You can isolate these values by using the STYPE_MASK value.
        # Disk drive.
        "STYPE_DISKTREE": 0x0,

        # Print queue.
        "STYPE_PRINTQ": 0x1,

        # Communication device.
        "STYPE_DEVICE": 0x2,

        # Interprocess communication (IPC).
        "STYPE_IPC": 0x3,

        ## In addition, one or both of the following values may be specified.
        # Special share reserved for interprocess communication (IPC$) or remote administration of the server (ADMIN$).
        # Can also refer to administrative shares such as C$, D$, E$, and so forth. For more information, see Network Share Functions.
        "STYPE_SPECIAL": 0x80000000,

        # A temporary share.
        "STYPE_TEMPORARY": 0x40000000
    }
    flags = []
    if (stype_value & 0b11) == known_flags["STYPE_DISKTREE"]:
        flags.append("STYPE_DISKTREE")
    elif (stype_value & 0b11) == known_flags["STYPE_PRINTQ"]:
        flags.append("STYPE_PRINTQ")
    elif (stype_value & 0b11) == known_flags["STYPE_DEVICE"]:
        flags.append("STYPE_DEVICE")
    elif (stype_value & 0b11) == known_flags["STYPE_IPC"]:
        flags.append("STYPE_IPC")
    if (stype_value & known_flags["STYPE_SPECIAL"]) == 0:
        flags.append("STYPE_SPECIAL")
    if (stype_value & known_flags["STYPE_TEMPORARY"]) == 0:
        flags.append("STYPE_TEMPORARY")
    return flags


def get_domain_computers(ldap_server, ldap_session):
    page_size = 1000
    # Controls
    # https://docs.microsoft.com/en-us/openspecs/windows_protocols/ms-adts/3c5e87db-4728-4f29-b164-01dd7d7391ea
    LDAP_PAGED_RESULT_OID_STRING = "1.2.840.113556.1.4.319"
    # https://docs.microsoft.com/en-us/openspecs/windows_protocols/ms-adts/f14f3610-ee22-4d07-8a24-1bf1466cba5f
    LDAP_SERVER_NOTIFICATION_OID = "1.2.840.113556.1.4.528"
    results = {}

    target_dn = ldap_server.info.other["defaultNamingContext"]

    # https://ldap3.readthedocs.io/en/latest/searches.html#the-search-operation
    paged_response = True
    paged_cookie = None
    while paged_response == True:
        ldap_session.search(
            target_dn, "(objectCategory=computer)", attributes=["dNSHostName", "sAMAccountName"],
            size_limit=0, paged_size=page_size, paged_cookie=paged_cookie
        )
        #
        if "controls" in ldap_session.result.keys():
            if LDAP_PAGED_RESULT_OID_STRING in ldap_session.result["controls"].keys():
                next_cookie = ldap_session.result["controls"][LDAP_PAGED_RESULT_OID_STRING]["value"]["cookie"]
                if len(next_cookie) == 0:
                    paged_response = False
                else:
                    paged_response = True
                    paged_cookie = next_cookie
            else:
                paged_response = False
        else:
            paged_response = False
        #
        for entry in ldap_session.response:
            if entry['type'] != 'searchResEntry':
                continue
            results[entry['dn']] = {
                'dNSHostName': entry["attributes"]['dNSHostName'],
                'sAMAccountName': entry["attributes"]['sAMAccountName']
            }
    return results


def parse_args():
    print("FindUncommonShares v%s - by @podalirius_\n" % VERSION)

    parser = argparse.ArgumentParser(add_help=True, description='Find uncommon SMB shares on remote machines.')
    parser.add_argument('--use-ldaps', action='store_true', help='Use LDAPS instead of LDAP')
    parser.add_argument("-q", "--quiet", dest="quiet", action="store_true", default=False, help="Show no information at all.")
    parser.add_argument("--debug", dest="debug", action="store_true", default=False, help="Debug mode.")
    parser.add_argument("-no-colors", dest="colors", action="store_false", default=True, help="Disables colored output mode")
    parser.add_argument("-I", "--ignore-hidden-shares", dest="ignore_hidden_shares", action="store_true", default=False, help="Ignores hidden shares (shares ending with $)")
    parser.add_argument("-t", "--threads", dest="threads", action="store", type=int, default=20, required=False, help="Number of threads (default: 20)")

    output = parser.add_argument_group('Output files')
    output.add_argument("--export-xlsx", dest="export_xlsx", type=str, default=None, required=False, help="Output XLSX file to store the results in.")
    output.add_argument("--export-json", dest="export_json", type=str, default=None, required=False, help="Output JSON file to store the results in.")
    output.add_argument("--export-sqlite", dest="export_sqlite", type=str, default=None, required=False, help="Output SQLITE3 file to store the results in.")

    authconn = parser.add_argument_group('Authentication & connection')
    authconn.add_argument('--dc-ip', required=True, action='store', metavar="ip address", help='IP Address of the domain controller or KDC (Key Distribution Center) for Kerberos. If omitted it will use the domain part (FQDN) specified in the identity parameter')
    authconn.add_argument("-d", "--domain", dest="auth_domain", metavar="DOMAIN", action="store", default="", help="(FQDN) domain to authenticate to")
    authconn.add_argument("-u", "--user", dest="auth_username", metavar="USER", action="store", default="", help="user to authenticate with")

    secret = parser.add_argument_group("Credentials")
    cred = secret.add_mutually_exclusive_group()
    cred.add_argument("--no-pass", default=False, action="store_true", help="Don't ask for password (useful for -k)")
    cred.add_argument("-p", "--password", dest="auth_password", metavar="PASSWORD", action="store", default="", help="Password to authenticate with")
    cred.add_argument("-H", "--hashes", dest="auth_hashes", action="store", metavar="[LMHASH:]NTHASH", help='NT/LM hashes, format is LMhash:NThash')
    cred.add_argument("--aes-key", dest="auth_key", action="store", metavar="hex key", help='AES key to use for Kerberos Authentication (128 or 256 bits)')
    secret.add_argument("-k", "--kerberos", dest="use_kerberos", action="store_true", help='Use Kerberos authentication. Grabs credentials from .ccache file (KRB5CCNAME) based on target parameters. If valid credentials cannot be found, it will use the ones specified in the command line')

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    options = parser.parse_args()

    if options.auth_password is None and options.no_pass == False:
        from getpass import getpass
        options.auth_password = getpass("Password:")

    return options


def get_machine_name(options, domain):
    if options.dc_ip is not None:
        s = SMBConnection(options.dc_ip, options.dc_ip)
    else:
        s = SMBConnection(domain, domain)
    try:
        s.login('', '')
    except Exception:
        if s.getServerName() == '':
            raise Exception('Error while anonymous logging into %s' % domain)
    else:
        s.logoff()
    return s.getServerName()


def init_ldap_connection(target, tls_version, options, domain, username, password, lmhash, nthash):
    user = '%s\\%s' % (domain, username)
    if tls_version is not None:
        use_ssl = True
        port = 636
        tls = ldap3.Tls(validate=ssl.CERT_NONE, version=tls_version)
    else:
        use_ssl = False
        port = 389
        tls = None
    ldap_server = ldap3.Server(target, get_info=ldap3.ALL, port=port, use_ssl=use_ssl, tls=tls)

    if options.use_kerberos:
        ldap_session = ldap3.Connection(ldap_server)
        ldap_session.bind()
        ldap3_kerberos_login(ldap_session, target, username, password, domain, lmhash, nthash, options.auth_key, kdcHost=options.dc_ip)
    elif options.auth_hashes is not None:
        if lmhash == "":
            lmhash = "aad3b435b51404eeaad3b435b51404ee"
        ldap_session = ldap3.Connection(ldap_server, user=user, password=lmhash + ":" + nthash, authentication=ldap3.NTLM, auto_bind=True)
    else:
        ldap_session = ldap3.Connection(ldap_server, user=user, password=password, authentication=ldap3.NTLM, auto_bind=True)

    return ldap_server, ldap_session


def init_ldap_session(options, domain, username, password, lmhash, nthash):
    if options.use_kerberos:
        target = get_machine_name(options, domain)
    else:
        if options.dc_ip is not None:
            target = options.dc_ip
        else:
            target = domain

    if options.use_ldaps is True:
        try:
            return init_ldap_connection(target, ssl.PROTOCOL_TLSv1_2, options, domain, username, password, lmhash, nthash)
        except ldap3.core.exceptions.LDAPSocketOpenError:
            return init_ldap_connection(target, ssl.PROTOCOL_TLSv1, options, domain, username, password, lmhash, nthash)
    else:
        return init_ldap_connection(target, None, options, domain, username, password, lmhash, nthash)


def ldap3_kerberos_login(connection, target, user, password, domain='', lmhash='', nthash='', aesKey='', kdcHost=None, TGT=None, TGS=None, useCache=True, debug=False):
    from pyasn1.codec.ber import encoder, decoder
    from pyasn1.type.univ import noValue
    """
    logins into the target system explicitly using Kerberos. Hashes are used if RC4_HMAC is supported.
    :param string user: username
    :param string password: password for the user
    :param string domain: domain where the account is valid for (required)
    :param string lmhash: LMHASH used to authenticate using hashes (password is not used)
    :param string nthash: NTHASH used to authenticate using hashes (password is not used)
    :param string aesKey: aes256-cts-hmac-sha1-96 or aes128-cts-hmac-sha1-96 used for Kerberos authentication
    :param string kdcHost: hostname or IP Address for the KDC. If None, the domain will be used (it needs to resolve tho)
    :param struct TGT: If there's a TGT available, send the structure here and it will be used
    :param struct TGS: same for TGS. See smb3.py for the format
    :param bool useCache: whether or not we should use the ccache for credentials lookup. If TGT or TGS are specified this is False
    :return: True, raises an Exception if error.
    """

    if lmhash != '' or nthash != '':
        if len(lmhash) % 2:
            lmhash = '0' + lmhash
        if len(nthash) % 2:
            nthash = '0' + nthash
        try:  # just in case they were converted already
            lmhash = binascii.unhexlify(lmhash)
            nthash = binascii.unhexlify(nthash)
        except TypeError:
            pass

    # Importing down here so pyasn1 is not required if kerberos is not used.
    from impacket.krb5.ccache import CCache
    from impacket.krb5.asn1 import AP_REQ, Authenticator, TGS_REP, seq_set
    from impacket.krb5.kerberosv5 import getKerberosTGT, getKerberosTGS
    from impacket.krb5 import constants
    from impacket.krb5.types import Principal, KerberosTime, Ticket
    import datetime

    if TGT is not None or TGS is not None:
        useCache = False

    if useCache:
        try:
            ccache = CCache.loadFile(os.getenv('KRB5CCNAME'))
        except Exception as e:
            # No cache present
            print(e)
            pass
        else:
            # retrieve domain information from CCache file if needed
            if domain == '':
                domain = ccache.principal.realm['data'].decode('utf-8')
                if debug:
                    print('[debug] Domain retrieved from CCache: %s' % domain)

            if debug:
                print('[debug] Using Kerberos Cache: %s' % os.getenv('KRB5CCNAME'))
            principal = 'ldap/%s@%s' % (target.upper(), domain.upper())

            creds = ccache.getCredential(principal)
            if creds is None:
                # Let's try for the TGT and go from there
                principal = 'krbtgt/%s@%s' % (domain.upper(), domain.upper())
                creds = ccache.getCredential(principal)
                if creds is not None:
                    TGT = creds.toTGT()
                    if debug:
                        print('[debug] Using TGT from cache')
                else:
                    if debug:
                        print('[debug] No valid credentials found in cache')
            else:
                TGS = creds.toTGS(principal)
                if debug:
                    print('[debug] Using TGS from cache')

            # retrieve user information from CCache file if needed
            if user == '' and creds is not None:
                user = creds['client'].prettyPrint().split(b'@')[0].decode('utf-8')
                if debug:
                    print('[debug] Username retrieved from CCache: %s' % user)
            elif user == '' and len(ccache.principal.components) > 0:
                user = ccache.principal.components[0]['data'].decode('utf-8')
                if debug:
                    print('[debug] Username retrieved from CCache: %s' % user)

    # First of all, we need to get a TGT for the user
    userName = Principal(user, type=constants.PrincipalNameType.NT_PRINCIPAL.value)
    if TGT is None:
        if TGS is None:
            tgt, cipher, oldSessionKey, sessionKey = getKerberosTGT(userName, password, domain, lmhash, nthash, aesKey, kdcHost)
    else:
        tgt = TGT['KDC_REP']
        cipher = TGT['cipher']
        sessionKey = TGT['sessionKey']

    if TGS is None:
        serverName = Principal('ldap/%s' % target, type=constants.PrincipalNameType.NT_SRV_INST.value)
        tgs, cipher, oldSessionKey, sessionKey = getKerberosTGS(serverName, domain, kdcHost, tgt, cipher, sessionKey)
    else:
        tgs = TGS['KDC_REP']
        cipher = TGS['cipher']
        sessionKey = TGS['sessionKey']

        # Let's build a NegTokenInit with a Kerberos REQ_AP

    blob = SPNEGO_NegTokenInit()

    # Kerberos
    blob['MechTypes'] = [TypesMech['MS KRB5 - Microsoft Kerberos 5']]

    # Let's extract the ticket from the TGS
    tgs = decoder.decode(tgs, asn1Spec=TGS_REP())[0]
    ticket = Ticket()
    ticket.from_asn1(tgs['ticket'])

    # Now let's build the AP_REQ
    apReq = AP_REQ()
    apReq['pvno'] = 5
    apReq['msg-type'] = int(constants.ApplicationTagNumbers.AP_REQ.value)

    opts = []
    apReq['ap-options'] = constants.encodeFlags(opts)
    seq_set(apReq, 'ticket', ticket.to_asn1)

    authenticator = Authenticator()
    authenticator['authenticator-vno'] = 5
    authenticator['crealm'] = domain
    seq_set(authenticator, 'cname', userName.components_to_asn1)
    now = datetime.datetime.utcnow()

    authenticator['cusec'] = now.microsecond
    authenticator['ctime'] = KerberosTime.to_asn1(now)

    encodedAuthenticator = encoder.encode(authenticator)

    # Key Usage 11
    # AP-REQ Authenticator (includes application authenticator
    # subkey), encrypted with the application session key
    # (Section 5.5.1)
    encryptedEncodedAuthenticator = cipher.encrypt(sessionKey, 11, encodedAuthenticator, None)

    apReq['authenticator'] = noValue
    apReq['authenticator']['etype'] = cipher.enctype
    apReq['authenticator']['cipher'] = encryptedEncodedAuthenticator

    blob['MechToken'] = encoder.encode(apReq)

    request = ldap3.operation.bind.bind_operation(connection.version, ldap3.SASL, user, None, 'GSS-SPNEGO',
                                                  blob.getData())

    # Done with the Kerberos saga, now let's get into LDAP
    if connection.closed:  # try to open connection if closed
        connection.open(read_server_info=False)

    connection.sasl_in_progress = True
    response = connection.post_send_single_response(connection.send('bindRequest', request, None))
    connection.sasl_in_progress = False
    if response[0]['result'] != 0:
        raise Exception(response)

    connection.bound = True

    return True


def init_smb_session(options, target_ip, domain, username, password, address, lmhash, nthash, port=445, debug=False):
    smbClient = SMBConnection(address, target_ip, sess_port=int(port))
    dialect = smbClient.getDialect()
    if dialect == SMB_DIALECT:
        if debug:
            print("[debug] SMBv1 dialect used")
    elif dialect == SMB2_DIALECT_002:
        if debug:
            print("[debug] SMBv2.0 dialect used")
    elif dialect == SMB2_DIALECT_21:
        if debug:
            print("[debug] SMBv2.1 dialect used")
    else:
        if debug:
            print("[debug] SMBv3.0 dialect used")
    if options.use_kerberos is True:
        smbClient.kerberosLogin(username, password, domain, lmhash, nthash, options.aesKey, options.dc_ip)
    else:
        smbClient.login(username, password, domain, lmhash, nthash)
    if smbClient.isGuestSession() > 0:
        if debug:
            print("[debug] GUEST Session Granted")
    else:
        if debug:
            print("[debug] USER Session Granted")
    return smbClient


def worker(options, target_name, domain, username, password, address, lmhash, nthash, results, lock):
    target_ip = nslookup.Nslookup(dns_servers=[options.dc_ip], verbose=options.debug).dns_lookup(target_name).answer
    if len(target_ip) != 0:
        target_ip = target_ip[0]
        try:
            smbClient = init_smb_session(options, target_ip, domain, username, password, address, lmhash, nthash)
            resp = smbClient.listShares()
            for share in resp:
                # SHARE_INFO_1 structure (lmshare.h)
                # https://docs.microsoft.com/en-us/windows/win32/api/lmshare/ns-lmshare-share_info_1
                sharename = share['shi1_netname'][:-1]
                sharecomment = share['shi1_remark'][:-1]
                sharetype = share['shi1_type']

                lock.acquire()
                if target_name not in results.keys():
                    results[target_name] = []
                results[target_name].append(
                    {
                        "computer": {
                            "fqdn": target_name,
                            "ip": target_ip
                        },
                        "share": {
                            "name": sharename,
                            "comment": sharecomment,
                            "hidden": (True if sharename.endswith('$') else False),
                            "uncpath": "\\".join(['', '', target_ip, sharename, '']),
                            "type": {
                                "stype_value": sharetype,
                                "stype_flags": STYPE_MASK(sharetype)
                            }
                        }
                    }
                )
                lock.release()

                if sharename not in COMMON_SHARES:
                    if not options.quiet:
                        if len(sharecomment) != 0:
                            if options.colors:
                                if sharename.endswith('$'):
                                    if not options.ignore_hidden_shares:
                                        print("[>] Found '\x1b[94m%s\x1b[0m' on '\x1b[96m%s\x1b[0m' (comment: '\x1b[95m%s\x1b[0m')" % (sharename, address, sharecomment))
                                else:
                                    print("[>] Found '\x1b[93m%s\x1b[0m' on '\x1b[96m%s\x1b[0m' (comment: '\x1b[95m%s\x1b[0m')" % (sharename, address, sharecomment))
                            else:
                                print("[>] Found '%s' on '%s' (comment: '%s')" % (sharename, address, sharecomment))
                        else:
                            if options.colors:
                                if sharename.endswith('$'):
                                    if not options.ignore_hidden_shares:
                                        print("[>] Found '\x1b[94m%s\x1b[0m' on '\x1b[96m%s\x1b[0m'" % (sharename, address))
                                else:
                                    print("[>] Found '\x1b[93m%s\x1b[0m' on '\x1b[96m%s\x1b[0m'" % (sharename, address))
                            else:
                                if sharename.endswith('$'):
                                    if not options.ignore_hidden_shares:
                                        print("[>] Found '%s' on '%s'" % (sharename, address))
                                else:
                                    print("[>] Found '%s' on '%s'" % (sharename, address))
                elif options.debug and not options.quiet:
                    if len(sharecomment) != 0:
                        if options.colors:
                            if sharename.endswith('$') and not options.ignore_hidden_shares:
                                print("[>] Skipping common share '\x1b[94m%s\x1b[0m' on '\x1b[96m%s\x1b[0m' (comment: '\x1b[95m%s\x1b[0m')" % (sharename, address, sharecomment))
                            else:
                                print("[>] Skipping common share '\x1b[93m%s\x1b[0m' on '\x1b[96m%s\x1b[0m' (comment: '\x1b[95m%s\x1b[0m')" % (sharename, address, sharecomment))
                        else:
                            print("[>] Skipping common share '%s' on '%s' (comment: '%s')" % (sharename, address, sharecomment))
                    else:
                        if options.colors:
                            if sharename.endswith('$') and not options.ignore_hidden_shares:
                                print("[>] Skipping common share '\x1b[94m%s\x1b[0m' on '\x1b[96m%s\x1b[0m'" % (sharename, address))
                            else:
                                print("[>] Skipping common share '\x1b[93m%s\x1b[0m' on '\x1b[96m%s\x1b[0m'" % (sharename, address))
                        else:
                            if sharename.endswith('$'):
                                if not options.ignore_hidden_shares:
                                    print("[>] Skipping common share '%s' on '%s'" % (sharename, address))
                            else:
                                print("[>] Skipping common share '%s' on '%s'" % (sharename, address))

        except Exception as e:
            if options.debug:
                print(e)


if __name__ == '__main__':
    options = parse_args()

    auth_lm_hash = ""
    auth_nt_hash = ""
    if options.auth_hashes is not None:
        if ":" in options.auth_hashes:
            auth_lm_hash = options.auth_hashes.split(":")[0]
            auth_nt_hash = options.auth_hashes.split(":")[1]
        else:
            auth_nt_hash = options.auth_hashes

    ldap_server, ldap_session = init_ldap_session(
        options=options,
        domain=options.auth_domain,
        username=options.auth_username,
        password=options.auth_password,
        lmhash=auth_lm_hash,
        nthash=auth_nt_hash
    )

    if not options.quiet:
        print("[>] Extracting all computers ...")
    computers = get_domain_computers(ldap_server, ldap_session)

    if not options.quiet:
        print("[+] Found %d computers in the domain. \n" % len(computers.keys()))
        print("[>] Enumerating shares ...")

    results = {}

    # Setup thread lock to properly write in the file
    lock = threading.Lock()
    # Waits for all the threads to be completed
    with ThreadPoolExecutor(max_workers=min(options.threads, len(computers.keys()))) as tp:
        for ck in computers.keys():
            computer = computers[ck]
            tp.submit(worker, options, computer['dNSHostName'], options.auth_domain, options.auth_username, options.auth_password, computer['dNSHostName'], auth_lm_hash, auth_nt_hash, results, lock)

    if options.export_json is not None:
        print("[>] Exporting results to %s ... " % options.export_json, end="")
        sys.stdout.flush()
        basepath = os.path.dirname(options.export_json)
        filename = os.path.basename(options.export_json)
        if basepath not in [".", ""]:
            if not os.path.exists(basepath):
                os.makedirs(basepath)
            path_to_file = basepath + os.path.sep + filename
        else:
            path_to_file = filename
        f = open(path_to_file, "w")
        f.write(json.dumps(results, indent=4)+"\n")
        f.close()
        print("done.")

    if options.export_xlsx is not None:
        print("[>] Exporting results to %s ... " % options.export_xlsx, end="")
        sys.stdout.flush()
        basepath = os.path.dirname(options.export_xlsx)
        filename = os.path.basename(options.export_xlsx)
        if basepath not in [".", ""]:
            if not os.path.exists(basepath):
                os.makedirs(basepath)
            path_to_file = basepath + os.path.sep + filename
        else:
            path_to_file = filename
        workbook = xlsxwriter.Workbook(path_to_file)
        worksheet = workbook.add_worksheet()

        header_format = workbook.add_format({'bold': 1})
        header_fields = ["Computer FQDN", "Computer IP", "Share name", "Share comment", "Is hidden"]
        for k in range(len(header_fields)):
            worksheet.set_column(k, k + 1, len(header_fields[k]) + 3)
        worksheet.set_row(0, 20, header_format)
        worksheet.write_row(0, 0, header_fields)

        row_id = 1
        for computername in results.keys():
            computer = results[computername]
            for share in computer:
                data = [share["computer"]["fqdn"], share["computer"]["ip"], share["share"]["name"], share["share"]["comment"], share["share"]["hidden"]]
                worksheet.write_row(row_id, 0, data)
                row_id += 1
        worksheet.autofilter(0, 0, row_id, len(header_fields)-1)
        workbook.close()
        print("done.")

    if options.export_sqlite is not None:
        print("[>] Exporting results to %s ..." % options.export_sqlite, end="")
        sys.stdout.flush()
        basepath = os.path.dirname(options.export_sqlite)
        filename = os.path.basename(options.export_sqlite)
        if basepath not in [".", ""]:
            if not os.path.exists(basepath):
                os.makedirs(basepath)
            path_to_file = basepath + os.path.sep + filename
        else:
            path_to_file = filename

        conn = sqlite3.connect(path_to_file)
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS shares(fqdn VARCHAR(255), ip VARCHAR(255), shi1_netname VARCHAR(255), shi1_remark VARCHAR(255), shi1_type INTEGER);")
        for computername in results.keys():
            for share in results[computername]:
                cursor.execute("INSERT INTO shares VALUES (?, ?, ?, ?, ?)", (
                        share["computer"]["fqdn"],
                        share["computer"]["ip"],
                        share["share"]["name"],
                        share["share"]["comment"],
                        share["share"]["type"]["stype_value"]
                    )
                )
        conn.commit()
        conn.close()
        print("done.")

