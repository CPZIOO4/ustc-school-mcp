"""Stateful synthetic IMAP server for reversible mail workflow tests (no network)."""
from email.message import EmailMessage
from school_mcp.mail.config import MailError
from school_mcp.mail.parsing import decode_mailbox, encode_mailbox


def message(subject='课程通知', body='本周课程安排。', sender='office@example.edu', identifier='<one@example.edu>'):
    value = EmailMessage()
    value['Subject'], value['From'] = subject, sender
    value['To'], value['Message-ID'] = 'student@mail.ustc.edu.cn', identifier
    value['Date'] = 'Tue, 06 Oct 2026 10:00:00 +0800'
    value.set_content(body)
    return value


class MemoryIMAP:
    def __init__(self):
        self.mailboxes = {'INBOX': {1: (message().as_bytes(), set())}, '归档文件夹': {}, '归档文件夹/课程': {}, 'Other': {}}
        self.validities = {name: index + 100 for index, name in enumerate(self.mailboxes)}
        self.selected, self.readonly = 'INBOX', True
        self.calls, self.mapping, self.next_uid = [], None, 50
        self.fail_after = None
        self.supports_uidplus = True
        self.copy_mapping_override = None

    def login(self, *args): return 'OK', []
    def logout(self): return 'BYE', []

    def capability(self):
        return 'OK', [b'IMAP4REV1 UIDPLUS' if self.supports_uidplus else b'IMAP4REV1']

    def list(self):
        return 'OK', [f'() "/" "{encode_mailbox(n)}"'.encode() for n in self.mailboxes]

    def create(self, name):
        name = decode_mailbox(name.strip('"'))
        self.mailboxes[name] = {}
        self.validities[name] = 100 + len(self.mailboxes)
        self.calls.append(('CREATE', name))
        return 'OK', []

    def select(self, name, readonly=True):
        self.selected = decode_mailbox(name.strip('"'))
        self.readonly = readonly
        self.calls.append(('SELECT', self.selected, readonly))
        return 'OK', [str(len(self.mailboxes[self.selected])).encode()]

    def response(self, name):
        return name, [self.mapping if name == 'COPYUID' else str(self.validities[self.selected]).encode()]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        mailbox = self.mailboxes[self.selected]
        if command == 'SEARCH':
            if args[0] == 'UID':
                uid = int(args[1])
                return 'OK', [str(uid).encode() if uid in mailbox else b'']
            return 'OK', [b' '.join(str(v).encode() for v in sorted(mailbox))]
        uid = int(args[0])
        if command == 'FETCH':
            if uid not in mailbox: return 'OK', []
            raw, flags = mailbox[uid]
            if args[1] == '(RFC822.SIZE)':
                return 'OK', [f'1 (UID {uid} RFC822.SIZE {len(raw)})'.encode()]
            return 'OK', [(f'1 (UID {uid} FLAGS ({" ".join(flags)}))'.encode(), raw)]
        assert not self.readonly, 'Write on EXAMINE selection'
        if command == 'COPY':
            target = decode_mailbox(args[1].strip('"'))
            raw, flags = mailbox[uid]
            self.next_uid += 1
            self.mailboxes[target][self.next_uid] = (raw, set(flags))
            self.mapping = self.copy_mapping_override or f'{self.validities[target]} {uid} {self.next_uid}'.encode()
        elif command == 'STORE':
            raw, flags = mailbox[uid]
            flag = args[2].strip('()').lower()
            flags.add(flag) if args[1].startswith('+') else flags.discard(flag)
        elif command == 'EXPUNGE':
            assert len(args) == 1 and '\\deleted' in mailbox[uid][1]
            del mailbox[uid]
        else:
            raise AssertionError(command)
        if self.fail_after == command:
            raise MailError('Synthetic response lost')
        return 'OK', []
