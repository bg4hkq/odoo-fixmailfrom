# -*- coding: utf-8 -*-


from email import Encoders
from email.charset import Charset
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import COMMASPACE, formataddr, formatdate, getaddresses, make_msgid
import logging
import re
import smtplib
import threading

from odoo.exceptions import except_orm, UserError
from odoo.tools import html2text, ustr

from odoo import api, fields, models, tools, _

_logger = logging.getLogger(__name__)
_test_logger = logging.getLogger('/var/log/odoo/odoo.tests')



class MailDeliveryException(except_orm):
    """Specific exception subclass for mail delivery errors"""
    def __init__(self, name, value):
        super(MailDeliveryException, self).__init__(name, value)


class WriteToLogger(object):
    """debugging helper: behave as a fd and pipe to logger at the given level"""
    def __init__(self, logger, level=logging.DEBUG):
        self.logger = logger
        self.level = level

    def write(self, s):
        self.logger.log(self.level, s)

def try_coerce_ascii(string_utf8):
    """Attempts to decode the given utf8-encoded string
       as ASCII after coercing it to UTF-8, then return
       the confirmed 7-bit ASCII string.

       If the process fails (because the string
       contains non-ASCII characters) returns ``None``.
    """
    try:
        string_utf8.decode('ascii')
    except UnicodeDecodeError:
        return
    return string_utf8


def encode_header(header_text):
    """Returns an appropriate representation of the given header value,
       suitable for direct assignment as a header value in an
       email.message.Message. RFC2822 assumes that headers contain
       only 7-bit characters, so we ensure it is the case, using
       RFC2047 encoding when needed.

       :param header_text: unicode or utf-8 encoded string with header value
       :rtype: string | email.header.Header
       :return: if ``header_text`` represents a plain ASCII string,
                return the same 7-bit string, otherwise returns an email.header.Header
                that will perform the appropriate RFC2047 encoding of
                non-ASCII values.
    """
    if not header_text:
        return ""
    # convert anything to utf-8, suitable for testing ASCIIness, as 7-bit chars are
    # encoded as ASCII in utf-8
    header_text_utf8 = ustr(header_text).encode('utf-8')
    header_text_ascii = try_coerce_ascii(header_text_utf8)
    # if this header contains non-ASCII characters,
    # we'll need to wrap it up in a message.header.Header
    # that will take care of RFC2047-encoding it as
    # 7-bit string.
    return header_text_ascii or Header(header_text_utf8, 'utf-8')


def encode_header_param(param_text):
    """Returns an appropriate RFC2047 encoded representation of the given
       header parameter value, suitable for direct assignation as the
       param value (e.g. via Message.set_param() or Message.add_header())
       RFC2822 assumes that headers contain only 7-bit characters,
       so we ensure it is the case, using RFC2047 encoding when needed.

       :param param_text: unicode or utf-8 encoded string with header value
       :rtype: string
       :return: if ``param_text`` represents a plain ASCII string,
                return the same 7-bit string, otherwise returns an
                ASCII string containing the RFC2047 encoded text.
    """
    # For details see the encode_header() method that uses the same logic
    if not param_text:
        return ""
    param_text_utf8 = ustr(param_text).encode('utf-8')
    param_text_ascii = try_coerce_ascii(param_text_utf8)
    return param_text_ascii or Charset('utf8').header_encode(param_text_utf8)


address_pattern = re.compile(r'([^ ,<@]+@[^> ,]+)')

def extract_rfc2822_addresses(text):
    """Returns a list of valid RFC2822 addresses
       that can be found in ``source``, ignoring
       malformed ones and non-ASCII ones.
    """
    if not text:
        return []
    candidates = address_pattern.findall(ustr(text).encode('utf-8'))
    return filter(try_coerce_ascii, candidates)


def encode_rfc2822_address_header(header_text):
    """If ``header_text`` contains non-ASCII characters,
       attempts to locate patterns of the form
       ``"Name" <address@domain>`` and replace the
       ``"Name"`` portion by the RFC2047-encoded
       version, preserving the address part untouched.
    """
    def encode_addr(addr):
        name, email = addr
        if not try_coerce_ascii(name):
            name = str(Header(name, 'utf-8'))
        return formataddr((name, email))

    addresses = getaddresses([ustr(header_text).encode('utf-8')])
    return COMMASPACE.join(map(encode_addr, addresses))




class ir_mail_server(models.Model):
    _inherit = "ir.mail_server"

    @api.model
    def send_email(self, message, mail_server_id=None, smtp_server=None, smtp_port=None,
                   smtp_user=None, smtp_password=None, smtp_encryption=None, smtp_debug=False):
        """Sends an email directly (no queuing).

        No retries are done, the caller should handle MailDeliveryException in order to ensure that
        the mail is never lost.

        If the mail_server_id is provided, sends using this mail server, ignoring other smtp_* arguments.
        If mail_server_id is None and smtp_server is None, use the default mail server (highest priority).
        If mail_server_id is None and smtp_server is not None, use the provided smtp_* arguments.
        If both mail_server_id and smtp_server are None, look for an 'smtp_server' value in server config,
        and fails if not found.

        :param message: the email.message.Message to send. The envelope sender will be extracted from the
                        ``Return-Path`` (if present), or will be set to the default bounce address.
                        The envelope recipients will be extracted from the combined list of ``To``,
                        ``CC`` and ``BCC`` headers.
        :param mail_server_id: optional id of ir.mail_server to use for sending. overrides other smtp_* arguments.
        :param smtp_server: optional hostname of SMTP server to use
        :param smtp_encryption: optional TLS mode, one of 'none', 'starttls' or 'ssl' (see ir.mail_server fields for explanation)
        :param smtp_port: optional SMTP port, if mail_server_id is not passed
        :param smtp_user: optional SMTP user, if mail_server_id is not passed
        :param smtp_password: optional SMTP password to use, if mail_server_id is not passed
        :param smtp_debug: optional SMTP debug flag, if mail_server_id is not passed
        :return: the Message-ID of the message that was just sent, if successfully sent, otherwise raises
                 MailDeliveryException and logs root cause.
        """
        # Use the default bounce address **only if** no Return-Path was
        # provided by caller.  Caller may be using Variable Envelope Return
        # Path (VERP) to detect no-longer valid email addresses.
        smtp_from = message['Return-Path'] or self._get_default_bounce_address() or message['From']
        assert smtp_from, "The Return-Path or From header is required for any outbound email"

        # The email's "Envelope From" (Return-Path), and all recipient addresses must only contain ASCII characters.
        from_rfc2822 = extract_rfc2822_addresses(smtp_from)
        assert from_rfc2822, ("Malformed 'Return-Path' or 'From' address: %r - "
                              "It should contain one valid plain ASCII email") % smtp_from
        # use last extracted email, to support rarities like 'Support@MyComp <support@mycompany.com>'
        smtp_from = from_rfc2822[-1]
        email_to = message['To']
        email_cc = message['Cc']
        email_bcc = message['Bcc']

        smtp_to_list = filter(None, tools.flatten(map(extract_rfc2822_addresses, [email_to, email_cc, email_bcc])))
        assert smtp_to_list, self.NO_VALID_RECIPIENT

        x_forge_to = message['X-Forge-To']
        if x_forge_to:
            # `To:` header forged, e.g. for posting on mail.channels, to avoid confusion
            del message['X-Forge-To']
            del message['To']  # avoid multiple To: headers!
            message['To'] = x_forge_to

        # Do not actually send emails in testing mode!
        if getattr(threading.currentThread(), 'testing', False):
            _test_logger.info("skip sending email in test mode")
            return message['Message-Id']

        # Get SMTP Server Details from Mail Server
        mail_server = None
        if mail_server_id:
            mail_server = self.sudo().browse(mail_server_id)
        elif not smtp_server:
            mail_server = self.sudo().search([], order='sequence', limit=1)

        if mail_server:
            smtp_server = mail_server.smtp_host
            smtp_user = mail_server.smtp_user
            smtp_password = mail_server.smtp_pass
            smtp_port = mail_server.smtp_port
            smtp_encryption = mail_server.smtp_encryption
            smtp_debug = smtp_debug or mail_server.smtp_debug
        else:
            # we were passed an explicit smtp_server or nothing at all
            smtp_server = smtp_server or tools.config.get('smtp_server')
            smtp_port = tools.config.get('smtp_port', 25) if smtp_port is None else smtp_port

            smtp_user = smtp_user or tools.config.get('smtp_user')
            smtp_password = smtp_password or tools.config.get('smtp_password')
            if smtp_encryption is None and tools.config.get('smtp_ssl'):
                smtp_encryption = 'starttls'  # STARTTLS is the new meaning of the smtp_ssl flag as of v7.0

        if not smtp_server:
            raise UserError(_("Missing SMTP Server") + "\n" + _(
                "Please define at least one SMTP server, or provide the SMTP parameters explicitly."))

        #mod by Yuri
        message.replace_header('From', '%s <%s>' % (message['From'], smtp_user))
        if message.has_key('return-path'):
            message.replace_header('return-path', '%s' % (smtp_user,))
        else:
            message.add_header('return-path', '%s' % (smtp_user,))




        try:
            message_id = message['Message-Id']

            # Add email in Maildir if smtp_server contains maildir.
            if smtp_server.startswith('maildir:/'):
                from mailbox import Maildir
                maildir_path = smtp_server[8:]
                mdir = Maildir(maildir_path, factory=None, create=True)
                mdir.add(message.as_string(True))
                return message_id

            smtp = None


            try:
                smtp = self.connect(smtp_server, smtp_port, smtp_user, smtp_password, smtp_encryption or False,
                                    smtp_debug)
                smtp.sendmail(smtp_user, smtp_to_list, message.as_string())
            finally:
                if smtp is not None:
                    smtp.quit()
        except Exception as e:
            params = (ustr(smtp_server), e.__class__.__name__, ustr(e))
            msg = _("Mail delivery failed via SMTP server '%s'.\n%s: %s") % params
            _logger.info(msg)
            raise MailDeliveryException(_("Mail Delivery Failed"), msg)

        return message_id

    @api.onchange('smtp_encryption')
    def _onchange_encryption(self):
        result = {}
        if self.smtp_encryption == 'ssl':
            self.smtp_port = 465
            if not 'SMTP_SSL' in smtplib.__all__:
                result['warning'] = {
                    'title': _('Warning'),
                    'message': _('Your server does not seem to support SSL, you may want to try STARTTLS instead'),
                }
        else:
            self.smtp_port = 25
        return result