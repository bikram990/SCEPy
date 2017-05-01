from flask import Flask, abort, request, Response, g
import plistlib
from .ca import CertificateAuthority
from .storage import FileStorage
from base64 import b64decode, b64encode
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from asn1crypto.csr import CertificationRequestInfo
from .message import SCEPMessage
from .enums import MessageType, PKIStatus, FailInfo
from .builders import PKIMessageBuilder, Signer, create_degenerate_certificate
from .envelope import PKCSPKIEnvelopeBuilder

# from .admin import admin_app

CACAPS = ('POSTPKIOperation', 'SHA-256', 'AES')


class WSGIChunkedBodyCopy(object):
    """WSGI wrapper that handles chunked encoding of the request body. Copies
    de-chunked body to a WSGI environment variable called `body_copy` (so best
    not to use with large requests lest memory issues crop up."""
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        wsgi_input = environ.get('wsgi.input')
        if 'chunked' in environ.get('HTTP_TRANSFER_ENCODING', '') and \
                        environ.get('CONTENT_LENGTH', '') == '' and \
                wsgi_input:

            body = b''
            sz = int(wsgi_input.readline(), 16)
            while sz > 0:
                body += wsgi_input.read(sz + 2)[:-2]
                sz = int(wsgi_input.readline(), 16)

            environ['body_copy'] = body
            environ['wsgi.input'] = body

        return self.app(environ, start_response)


app = Flask(__name__)
app.config.from_object('scepy.default_settings')
app.config.from_envvar('SCEPY_SETTINGS', True)
app.wsgi_app = WSGIChunkedBodyCopy(app.wsgi_app)
# app.register_blueprint(admin_app)

with app.app_context():
    storage = FileStorage(app.config['CA_ROOT'])
    if storage.exists():
        g.ca = CertificateAuthority(storage)
    else:
        g.ca = CertificateAuthority.create(storage)

@app.route('/cgi-bin/pkiclient.exe', methods=['GET', 'POST'])
@app.route('/scep', methods=['GET', 'POST'])
@app.route('/', methods=['GET', 'POST'])
def scep():
    op = request.args.get('operation')
    ca = g.ca

    if op == 'GetCACert':
        certs = [ca.certificate]

        if len(certs) == 1 and not app.config.get('FORCE_DEGENERATE_FOR_SINGLE_CERT', False):
            return Response(certs[0].public_bytes(Encoding.DER), mimetype='application/x-x509-ca-cert')
        elif len(certs):
            raise ValueError('cryptography cannot produce degenerate pkcs7 certs')
            # p7_degenerate = degenerate_pkcs7_der(certs)
            # return Response(p7_degenerate, mimetype='application/x-x509-ca-ra-cert')
    elif op == 'GetCACaps':
        return '\n'.join(CACAPS)
    elif op == 'PKIOperation':
        if request.method == 'GET':
            msg = request.args.get('message')
            # note: OS X improperly encodes the base64 query param by not
            # encoding spaces as %2B and instead leaving them as +'s
            msg = b64decode(msg.replace(' ', '+'))
        elif request.method == 'POST':
            # workaround for Flask/Werkzeug lack of chunked handling
            if 'chunked' in request.headers.get('Transfer-Encoding', ''):
                msg = request.environ['body_copy']
            else:
                msg = request.data

        req = SCEPMessage.parse(msg)
        app.logger.debug('Received SCEPMessage, details follow')
        req.debug()

        if req.message_type == MessageType.PKCSReq:
            app.logger.debug('received PKCSReq SCEP message')

            cakey = ca.private_key
            cacert = ca.certificate

            der_req = req.get_decrypted_envelope_data(
                cacert,
                cakey,
            )

            cert_req = x509.load_der_x509_csr(der_req, backend=default_backend())
            req_info_bytes = cert_req.tbs_certrequest_bytes

            # Check the challenge password
            req_info = CertificationRequestInfo.load(req_info_bytes)
            for attr in req_info['attributes']:
                if attr['type'].native == 'challenge_password':
                    assert len(attr['values']) == 1
                    challenge_password = attr['values'][0].native
                    print("{:<20}: {}".format('Challenge Password', challenge_password))
                    break  # TODO: if challenge password fails send pkcs#7 with pki status failure

            # CA should persist all signed certs itself
            new_cert = ca.sign(cert_req)
            degenerate = create_degenerate_certificate(new_cert)
            with open('/tmp/degenerate.der', 'wb') as fd:
                fd.write(degenerate.dump())

            envelope, _, _ = PKCSPKIEnvelopeBuilder().encrypt(degenerate.dump()).add_recipient(
                req.certificates[0]).finalize()
            signer = Signer(cacert, cakey)

            reply = PKIMessageBuilder().message_type(
                MessageType.CertRep
            ).transaction_id(
                req.transaction_id
            ).pki_status(
                PKIStatus.SUCCESS
            ).recipient_nonce(
                req.sender_nonce
            ).pki_envelope(
                envelope
            ).certificates(new_cert).add_signer(signer).finalize()

            res = SCEPMessage.parse(reply.dump())
            app.logger.debug('Reply with CertRep, details follow')
            res.debug()

            with open('/tmp/reply.bin', 'wb') as fd:
                fd.write(reply.dump())

            return Response(reply.dump(), mimetype='application/x-pki-message')
        else:
            app.logger.error('unhandled SCEP message type: %d', req.message_type)
            return ''
    else:
        abort(404, 'unknown SCEP operation')


@app.route('/mobileconfig')
def mobileconfig():
    """Quick and dirty SCEP enrollment mobileconfiguration profile."""
    my_url = 'http://localhost:5000'

    profile = {
        'PayloadType': 'Configuration',
        'PayloadDisplayName': 'SCEPy Enrolment Profile',
        'PayloadVersion': 1,
        'PayloadIdentifier': 'com.github.mosen.scepy',
        'PayloadUUID': '7F165A7B-FACE-4A6E-8B56-CA3CC2E9D0BF',
        'PayloadContent': [
            {
                'PayloadType': 'com.apple.security.scep',
                'PayloadVersion': 1,
                'PayloadIdentifier': 'com.github.mosen.scepy.scep',
                'PayloadUUID': '16D129CA-DA22-4749-82D5-A28201622555',
                'PayloadDisplayName': 'SCEPy Enrolment Payload',
                'PayloadContent': {
                    'URL': my_url,
                    'Name': 'SCEPY-CA'
                }
            }
        ]
    }

    return plistlib.dumps(profile), {'Content-Type': 'application/x-apple-aspen-config'}
