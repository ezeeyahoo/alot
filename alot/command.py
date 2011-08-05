"""
This file is part of alot.

Alot is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the
Free Software Foundation, either version 3 of the License, or (at your
option) any later version.

Alot is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
for more details.

You should have received a copy of the GNU General Public License
along with notmuch.  If not, see <http://www.gnu.org/licenses/>.

Copyright (C) 2011 Patrick Totzke <patricktotzke@gmail.com>
"""
import os
import code
import logging
import threading
import subprocess
import email
import tempfile
from email.parser import Parser
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email import Charset
from email.header import Header

import buffer
import settings
from db import DatabaseROError
from db import DatabaseLockedError
from completion import ContactsCompleter
from completion import AccountCompleter
from message import decode_to_unicode
from message import decode_header
from message import encode_header


class Command:
    """base class for commands"""
    def __init__(self, prehook=None, posthook=None, **ignored):
        self.prehook = prehook
        self.posthook = posthook
        self.undoable = False
        self.help = self.__doc__

    def apply(self, caller):
        pass


class ExitCommand(Command):
    """shuts the MUA down cleanly"""
    def apply(self, ui):
        ui.shutdown()


class OpenThreadCommand(Command):
    """open a new thread-view buffer"""
    def __init__(self, thread=None, **kwargs):
        self.thread = thread
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        if not self.thread:
            self.thread = ui.current_buffer.get_selected_thread()
        ui.logger.info('open thread view for %s' % self.thread)

        sb = buffer.ThreadBuffer(ui, self.thread)
        ui.buffer_open(sb)


class SearchCommand(Command):
    """open a new search buffer"""
    def __init__(self, query, force_new=False, **kwargs):
        """
        @param query initial querystring
        @param force_new True forces a new buffer
        """
        self.query = query
        self.force_new = force_new
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        if not self.force_new:
            open_searches = ui.get_buffers_of_type(buffer.SearchBuffer)
            to_be_focused = None
            for sb in open_searches:
                if sb.querystring == self.query:
                    to_be_focused = sb
            if to_be_focused:
                ui.buffer_focus(to_be_focused)
            else:
                ui.buffer_open(buffer.SearchBuffer(ui, self.query))
        else:
            ui.buffer_open(buffer.SearchBuffer(ui, self.query))


class PromptCommand(Command):
    def __init__(self, startstring=u'', **kwargs):
        self.startstring = startstring
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        ui.commandprompt(self.startstring)


class RefreshCommand(Command):
    """refreshes the current buffer"""
    def apply(self, ui):
        ui.current_buffer.rebuild()
        ui.update()


class ExternalCommand(Command):
    """
    calls external command
    """
    def __init__(self, commandstring, spawn=False, refocus=True,
                 in_thread=False, on_success=None, **kwargs):
        """
        :param commandstring: the command to call
        :type commandstring: str
        :param spawn: run command in a new terminal
        :type spawn: boolean
        :param refocus: refocus calling buffer after cmd termination
        :type refocus: boolean
        :param on_success: code to execute after command successfully exited
        :type on_success: callable
        """
        self.commandstring = commandstring
        self.spawn = spawn
        self.refocus = refocus
        self.in_thread = in_thread
        self.on_success = on_success
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        callerbuffer = ui.current_buffer

        def afterwards(data):
            if callable(self.on_success) and data == 'success':
                self.on_success()
            if self.refocus and callerbuffer in ui.buffers:
                ui.logger.info('refocussing')
                ui.buffer_focus(callerbuffer)

        write_fd = ui.mainloop.watch_pipe(afterwards)

        def thread_code(*args):
            cmd = self.commandstring
            if self.spawn:
                cmd = '%s %s' % (settings.config.get('general',
                                                      'terminal_cmd'),
                                  cmd)
            ui.logger.info('calling external command: %s' % cmd)
            returncode = subprocess.call(cmd, shell=True)
            if returncode == 0:
                os.write(write_fd, 'success')

        if self.in_thread:
            thread = threading.Thread(target=thread_code)
            thread.start()
        else:
            ui.mainloop.screen.stop()
            thread_code()
            ui.mainloop.screen.start()


class EditCommand(ExternalCommand):
    def __init__(self, path, spawn=None, **kwargs):
        self.path = path
        if spawn != None:
            self.spawn = spawn
        else:
            self.spawn = settings.config.getboolean('general', 'spawn_editor')
        editor_cmd = settings.config.get('general', 'editor_cmd')
        cmd = editor_cmd + ' ' + self.path
        ExternalCommand.__init__(self, cmd, spawn=self.spawn,
                                 in_thread=self.spawn,
                                 **kwargs)


class PythonShellCommand(Command):
    """
    opens an interactive shell for introspection
    """
    def apply(self, ui):
        ui.mainloop.screen.stop()
        code.interact(local=locals())
        ui.mainloop.screen.start()


class BufferCloseCommand(Command):
    """
    close a buffer
    @param buffer the selected buffer
    """
    def __init__(self, buffer=None, focussed=False, **kwargs):
        self.buffer = buffer
        self.focussed = focussed
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        if self.focussed:
            self.buffer = ui.current_buffer.get_selected_buffer()
        elif not self.buffer:
            self.buffer = ui.current_buffer
        ui.buffer_close(self.buffer)
        ui.buffer_focus(ui.current_buffer)


class BufferFocusCommand(Command):
    """
    focus a buffer
    @param buffer the selected buffer
    """
    def __init__(self, buffer=None, offset=0, **kwargs):
        self.buffer = buffer
        self.offset = offset
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        if self.offset:
            idx = ui.buffers.index(ui.current_buffer)
            num = len(ui.buffers)
            self.buffer = ui.buffers[(idx + self.offset) % num]
        else:
            if not self.buffer:
                self.buffer = ui.current_buffer.get_selected_buffer()
        ui.buffer_focus(self.buffer)


class OpenBufferlistCommand(Command):
    """
    open a bufferlist
    """
    def __init__(self, filtfun=None, **kwargs):
        self.filtfun = filtfun
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        blists = ui.get_buffers_of_type(buffer.BufferlistBuffer)
        if blists:
            ui.buffer_focus(blists[0])
        else:
            ui.buffer_open(buffer.BufferlistBuffer(ui, self.filtfun))


class TagListCommand(Command):
    """
    open a taglist
    """
    def __init__(self, filtfun=None, **kwargs):
        self.filtfun = filtfun
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        tags = ui.dbman.get_all_tags()
        buf = buffer.TagListBuffer(ui, tags, self.filtfun)
        ui.buffers.append(buf)
        buf.rebuild()
        ui.buffer_focus(buf)


class CommandPromptCommand(Command):
    """
    """
    def apply(self, ui):
        ui.commandprompt()


class FlushCommand(Command):
    """
    Flushes writes to the index. Retries until committed
    """
    def apply(self, ui):
        try:
            ui.dbman.flush()
        except DatabaseLockedError:
            timeout = settings.config.getint('general', 'flush_retry_timeout')

            def f(*args):
                self.apply(ui)
            ui.mainloop.set_alarm_in(timeout, f)
            ui.notify('index locked, will try again in %d secs' % timeout)
            ui.update()
            return


class ToggleThreadTagCommand(Command):
    """
    """
    def __init__(self, tag, thread=None, **kwargs):
        assert tag
        self.thread = thread
        self.tag = tag
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        if not self.thread:
            self.thread = ui.current_buffer.get_selected_thread()
        try:
            if self.tag in self.thread.get_tags():
                self.thread.remove_tags([self.tag])
            else:
                self.thread.add_tags([self.tag])
        except DatabaseROError:
            ui.notify('index in read-only mode', priority='error')
            return

        # flush index
        ui.apply_command(FlushCommand())

        # update current buffer
        # TODO: what if changes not yet flushed?
        cb = ui.current_buffer
        if isinstance(cb, buffer.SearchBuffer):
            # refresh selected threadline
            threadwidget = cb.get_selected_threadline()
            threadwidget.rebuild()  # rebuild and redraw the line
            #remove line from searchlist if thread doesn't match the query
            qs = "(%s) AND thread:%s" % (cb.querystring,
                                         self.thread.get_thread_id())
            msg_count = ui.dbman.count_messages(qs)
            if ui.dbman.count_messages(qs) == 0:
                ui.logger.debug('remove: %s' % self.thread)
                cb.threadlist.remove(threadwidget)
                cb.result_count -= self.thread.get_total_messages()
                ui.update()
        elif isinstance(cb, buffer.ThreadBuffer):
            pass


class ComposeCommand(Command):
    def __init__(self, mail=None, **kwargs):
        if not mail:
            self.mail = MIMEMultipart()
            self.mail.attach(MIMEText('', 'plain', 'UTF-8'))
        else:
            self.mail = mail
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        # TODO: fill with default header (per account)
        # get From header
        if not 'From' in self.mail:
            accounts = ui.accountman.get_accounts()
            if len(accounts) == 0:
                ui.notify('no accounts set')
                return
            elif len(accounts) == 1:
                a = accounts[0]
            else:
                cmpl = AccountCompleter(ui.accountman)
                fromaddress = ui.prompt(prefix='From>', completer=cmpl, tab=1)
                validaddresses = [a.address for a in accounts] + [None]
                while fromaddress not in validaddresses:
                    ui.notify('no account for this address. (<esc> cancels)')
                    fromaddress = ui.prompt(prefix='From>', completer=cmpl)
                if not fromaddress:
                    ui.notify('canceled')
                    return
                a = ui.accountman.get_account_by_address(fromaddress)
            self.mail['From'] = "%s <%s>" % (a.realname, a.address)

        #get To header
        if 'To' not in self.mail:
            to = ui.prompt(prefix='To>', completer=ContactsCompleter())
            self.mail['To'] = encode_header('to', to)
        if settings.config.getboolean('general', 'ask_subject') and \
           not 'Subject' in self.mail:
            subject = ui.prompt(prefix='Subject>')
            self.mail['Subject'] = encode_header('subject', subject)

        ui.apply_command(EnvelopeEditCommand(mail=self.mail))


# SEARCH
class RetagPromptCommand(Command):
    """start a commandprompt to retag selected threads' tags"""

    def apply(self, ui):
        thread = ui.current_buffer.get_selected_thread()
        initial_tagstring = ','.join(thread.get_tags())
        ui.commandprompt('retag ' + initial_tagstring)


class RetagCommand(Command):
    """tag selected thread"""

    def __init__(self, tagsstring=u'', **kwargs):
        self.tagsstring = tagsstring
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        thread = ui.current_buffer.get_selected_thread()
        initial_tagstring = ','.join(thread.get_tags())
        tags = filter(lambda x: x, self.tagsstring.split(','))
        ui.logger.info("got %s:%s" % (self.tagsstring, tags))
        try:
            thread.set_tags(tags)
        except DatabaseROError, e:
            ui.notify('index in read-only mode', priority='error')
            return

        # flush index
        ui.apply_command(FlushCommand())

        # refresh selected threadline
        sbuffer = ui.current_buffer
        threadwidget = sbuffer.get_selected_threadline()
        threadwidget.rebuild()  # rebuild and redraw the line


class RefineCommand(Command):
    """refine the query of the currently open searchbuffer"""

    def __init__(self, query=None, **kwargs):
        self.querystring = query
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        sbuffer = ui.current_buffer
        oldquery = sbuffer.querystring
        if self.querystring not in [None, oldquery]:
            sbuffer.querystring = self.querystring
            sbuffer = ui.current_buffer
            sbuffer.rebuild()
            ui.update()


class RefinePromptCommand(Command):
    """prompt to change current search buffers query"""

    def apply(self, ui):
        sbuffer = ui.current_buffer
        oldquery = sbuffer.querystring
        ui.commandprompt('refine ' + oldquery)


# THREAD
class ReplyCommand(Command):
    def __init__(self, groupreply=False, **kwargs):
        self.groupreply = groupreply
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        msg = ui.current_buffer.get_selected_message()
        mail = msg.get_email()
        # set body text
        mailcontent = '\nOn %s, %s wrote:\n' % (msg.get_datestring(),
                msg.get_author()[0])
        for line in msg.accumulate_body().splitlines():
            mailcontent += '>' + line + '\n'

        Charset.add_charset('utf-8', Charset.QP, Charset.QP, 'utf-8')
        bodypart = MIMEText(mailcontent.encode('utf-8'), 'plain', 'UTF-8')
        reply = MIMEMultipart()
        reply.attach(bodypart)

        # copy subject
        subject = mail['Subject']
        if not subject.startswith('Re:'):
            subject = 'Re: ' + subject
        reply['Subject'] = Header(subject.encode('utf-8'), 'UTF-8').encode()

        # set From
        my_addresses = ui.accountman.get_account_addresses()
        matched_address = ''
        in_to = [a for a in my_addresses if a in mail['To']]
        if in_to:
            matched_address = in_to[0]
        else:
            cc = mail.get('Cc', '') + mail.get('Bcc', '')
            in_cc = [a for a in my_addresses if a in cc]
            if in_cc:
                matched_address = in_cc[0]
        if matched_address:
            account = ui.accountman.get_account_by_address(matched_address)
            fromstring = '%s <%s>' % (account.realname, account.address)
            reply['From'] = encode_header('From', fromstring)

        # set To
        #reply['To'] = Header(mail['From'].encode('utf-8'), 'UTF-8').encode()
        del(reply['To'])
        if self.groupreply:
            cleared = self.clear_my_address(my_addresses, mail['To'])
            if cleared:
                logging.info(mail['From'] + ', ' + cleared)
                to = mail['From'] + ', ' + cleared
                reply['To'] = encode_header('To', to)
                logging.info(reply['To'])
            else:
                reply['To'] = encode_header('To', mail['From'])
            # copy cc and bcc for group-replies
            if 'Cc' in mail:
                cc = self.clear_my_address(my_addresses, mail['Cc'])
                reply['Cc'] = encode_header('Cc', cc)
            if 'Bcc' in mail:
                bcc = self.clear_my_address(my_addresses, mail['Bcc'])
                reply['Bcc'] = encode_header('Bcc', bcc)
        else:
            reply['To'] = encode_header('To', mail['From'])

        # set In-Reply-To header
        del(reply['In-Reply-To'])
        reply['In-Reply-To'] = '<%s>' % msg.get_message_id()

        # set References header
        old_references = mail['References']
        if old_references:
            old_references = old_references.split()
            references = old_references[-8:]
            if len(old_references) > 8:
                references = old_references[:1] + references
            references.append('<%s>' % msg.get_message_id())
            reply['References'] = ' '.join(references)
        else:
            reply['References'] = '<%s>' % msg.get_message_id()

        ui.apply_command(ComposeCommand(mail=reply))

    def clear_my_address(self, my_addresses, value):
        new_value = []
        for entry in value.split(','):
            if not [a for a in my_addresses if a in entry]:
                new_value.append(entry.strip())
        return ', '.join(new_value)


class ForwardCommand(Command):
    def __init__(self, inline=False, **kwargs):
        self.inline = inline
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        msg = ui.current_buffer.get_selected_message()
        mail = msg.get_email()

        reply = MIMEMultipart()
        Charset.add_charset('utf-8', Charset.QP, Charset.QP, 'utf-8')
        if self.inline:  # inline mode
            # set body text
            author = msg.get_author()[0]
            mailcontent = '\nForwarded message from %s:\n' % author
            for line in msg.accumulate_body().splitlines():
                mailcontent += '>' + line + '\n'

            bodypart = MIMEText(mailcontent.encode('utf-8'), 'plain', 'UTF-8')
            reply.attach(bodypart)

        else:  # attach original mode
            # create empty text msg
            bodypart = MIMEText('', 'plain', 'UTF-8')
            reply.attach(bodypart)
            # attach original msg
            reply.attach(mail)

        # copy subject
        subject = mail['Subject']
        subject = 'Fwd: ' + subject
        reply['Subject'] = Header(subject.encode('utf-8'), 'UTF-8').encode()

        # set From
        my_addresses = ui.accountman.get_account_addresses()
        matched_address = ''
        in_to = [a for a in my_addresses if a in mail['To']]
        if in_to:
            matched_address = in_to[0]
        else:
            cc = mail.get('Cc', '') + mail.get('Bcc', '')
            in_cc = [a for a in my_addresses if a in cc]
            if in_cc:
                matched_address = in_cc[0]
        if matched_address:
            account = ui.accountman.get_account_by_address(matched_address)
            fromstring = '%s <%s>' % (account.realname, account.address)
            reply['From'] = encode_header('From', fromstring)
        ui.apply_command(ComposeCommand(mail=reply))


class BounceMailCommand(Command):
    def apply(self, ui):
        msg = ui.current_buffer.get_selected_message()
        mail = msg.get_email()
        del(mail['To'])
        ui.apply_command(ComposeCommand(mail=mail))


class FoldMessagesCommand(Command):
    def __init__(self, all=False, visible=True, **kwargs):
        self.all = all
        self.visible = visible
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        lines = []
        if not self.all:
            lines.append(ui.current_buffer.get_selection())
        else:
            lines = ui.current_buffer.get_message_widgets()

        for widget in lines:
            # in case the thread is yet unread, remove this tag
            msg = widget.get_message()
            if 'unread' in msg.get_tags():
                msg.remove_tags(['unread'])
                ui.apply_command(FlushCommand())
                widget.rebuild()
            widget.fold(self.visible)


### ENVELOPE
class EnvelopeOpenCommand(Command):
    def __init__(self, mail=None, **kwargs):
        self.mail = mail
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        ui.buffer_open(buffer.EnvelopeBuffer(ui, mail=self.mail))


class EnvelopeEditCommand(Command):
    """re-edits mail in from envelope buffer"""
    def __init__(self, mail=None, **kwargs):
        self.mail = mail
        self.openNew = (mail != None)
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        Charset.add_charset('utf-8', Charset.QP, Charset.QP, 'utf-8')
        if not self.mail:
            self.mail = ui.current_buffer.get_email()

        def openEnvelopeFromTmpfile():
            f = open(tf.name)
            editor_input = f.read().decode('utf-8')

            #split editor out
            headertext, bodytext = editor_input.split('\n\n', 1)

            for line in headertext.splitlines():
                key, value = line.strip().split(':', 1)
                value = value.strip()
                del self.mail[key]  # ensure there is only one
                self.mail[key] = encode_header(key, value)

            if self.mail.is_multipart():
                for part in self.mail.walk():
                    if part.get_content_maintype() == 'text':
                        if 'Content-Transfer-Encoding' in part:
                            del(part['Content-Transfer-Encoding'])
                        part.set_payload(bodytext, 'utf-8')
                        break

            f.close()
            os.unlink(tf.name)
            if self.openNew:
                ui.apply_command(EnvelopeOpenCommand(mail=self.mail))
            else:
                ui.current_buffer.set_email(self.mail)

        # decode header
        edit_headers = ['Subject', 'To', 'From']
        headertext = u''
        for key in edit_headers:
            value = u''
            if key in self.mail:
                value = decode_header(self.mail[key])
            headertext += '%s: %s\n' % (key, value)

        if self.mail.is_multipart():
            for part in self.mail.walk():
                if part.get_content_maintype() == 'text':
                    bodytext = decode_to_unicode(part)
                    break
        else:
            bodytext = decode_to_unicode(self.mail)

        #write stuff to tempfile
        tf = tempfile.NamedTemporaryFile(delete=False)
        content = '%s\n\n%s' % (headertext,
                                bodytext)
        tf.write(content.encode('utf-8'))
        tf.flush()
        tf.close()
        cmd = EditCommand(tf.name, on_success=openEnvelopeFromTmpfile,
                          refocus=False)
        ui.apply_command(cmd)


class EnvelopeSetCommand(Command):
    """sets header fields of mail open in envelope buffer"""

    def __init__(self, key='', value=u'', replace=True, **kwargs):
        self.key = key
        self.value = encode_header(key, value)
        self.replace = replace
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        envelope = ui.current_buffer
        mail = envelope.get_email()
        if self.replace:
            del(mail[self.key])
        mail[self.key] = self.value
        envelope.rebuild()


class EnvelopeSendCommand(Command):
    def apply(self, ui):
        envelope = ui.current_buffer
        mail = envelope.get_email()
        frm = decode_header(mail.get('From'))
        sname, saddr = email.Utils.parseaddr(frm)
        account = ui.accountman.get_account_by_address(saddr)
        if account:
            clearme = ui.notify('sending..', timeout=-1, block=False)
            success, reason = account.sender.send_mail(mail)
            ui.clear_notify([clearme])
            if success:
                cmd = BufferCloseCommand(buffer=envelope)
                ui.apply_command(cmd)
                ui.notify('mail send successful')
            else:
                ui.notify('failed to send: %s' % reason, priority='error')
        else:
            ui.notify('failed to send: no account set up for %s' % saddr,
                      priority='error')


# TAGLIST
class TaglistSelectCommand(Command):
    def apply(self, ui):
        tagstring = ui.current_buffer.get_selected_tag()
        cmd = SearchCommand(query='tag:%s' % tagstring)
        ui.apply_command(cmd)


COMMANDS = {
    'search': {
        'refine': (RefineCommand, {}),
        'refineprompt': (RefinePromptCommand, {}),
        'openthread': (OpenThreadCommand, {}),
        'toggletag': (ToggleThreadTagCommand, {'tag': 'inbox'}),
        'retag': (RetagCommand, {}),
        'retagprompt': (RetagPromptCommand, {}),
    },
    'envelope': {
        'send': (EnvelopeSendCommand, {}),
        'reedit': (EnvelopeEditCommand, {}),
        'subject': (EnvelopeSetCommand, {'key': 'Subject'}),
        'to': (EnvelopeSetCommand, {'key': 'To'}),
    },
    'bufferlist': {
        'closefocussed': (BufferCloseCommand, {'focussed': True}),
        'openfocussed': (BufferFocusCommand, {}),
    },
    'taglist': {
        'select': (TaglistSelectCommand, {}),
    },
    'thread': {
        'reply': (ReplyCommand, {}),
        'groupreply': (ReplyCommand, {'groupreply': True}),
        'forward': (ForwardCommand, {}),
        'bounce': (BounceMailCommand, {}),
        'fold': (FoldMessagesCommand, {'visible': True}),
        'unfold': (FoldMessagesCommand, {'visible': False}),
    },
    'global': {
        'bnext': (BufferFocusCommand, {'offset': 1}),
        'bprevious': (BufferFocusCommand, {'offset': -1}),
        'bufferlist': (OpenBufferlistCommand, {}),
        'close': (BufferCloseCommand, {}),
        'commandprompt': (CommandPromptCommand, {}),
        'compose': (ComposeCommand, {}),
        'edit': (EditCommand, {}),
        'exit': (ExitCommand, {}),
        'flush': (FlushCommand, {}),
        'prompt': (PromptCommand, {}),
        'pyshell': (PythonShellCommand, {}),
        'refresh': (RefreshCommand, {}),
        'search': (SearchCommand, {}),
        'shellescape': (ExternalCommand, {}),
        'taglist': (TagListCommand, {}),
    }
}


def commandfactory(cmdname, mode='global', **kwargs):
    if cmdname in COMMANDS[mode]:
        (cmdclass, parms) = COMMANDS[mode][cmdname]
    elif cmdname in COMMANDS['global']:
        (cmdclass, parms) = COMMANDS['global'][cmdname]
    else:
        logging.error('there is no command %s' % cmdname)
    parms = parms.copy()
    parms.update(kwargs)
    for (key, value) in kwargs.items():
        if callable(value):
            parms[key] = value()
        else:
            parms[key] = value

    parms['prehook'] = settings.hooks.get('pre_' + cmdname)
    parms['posthook'] = settings.hooks.get('post_' + cmdname)

    logging.debug('cmd parms %s' % parms)
    return cmdclass(**parms)


def interpret_commandline(cmdline, mode):
    if not cmdline:
        return None
    logging.debug('mode:%s got commandline "%s"' % (mode, cmdline))
    args = cmdline.strip().split(' ', 1)
    cmd = args[0]
    if args[1:]:
        params = args[1]
    else:
        params = ''

    # unfold aliases
    if settings.config.has_option('command-aliases', cmd):
        cmd = settings.config.get('command-aliases', cmd)

    # allow to shellescape without a space after '!'
    if cmd.startswith('!'):
        params = cmd[1:] +' ' + params
        cmd = 'shellescape'

    # check if this command makes sense in current mode
    if cmd not in COMMANDS[mode] and cmd not in COMMANDS['global']:
        logging.debug('unknown command: %s' % (cmd))
        return None

    if cmd == 'search':
        return commandfactory(cmd, mode=mode, query=params)
    elif cmd == 'compose':
        return commandfactory(cmd, mode=mode, headers={'To': params})
    elif cmd == 'prompt':
        return commandfactory(cmd, mode=mode, startstring=params)
    elif cmd == 'refine':
        return commandfactory(cmd, mode=mode, query=params)
    elif cmd == 'retag':
        return commandfactory(cmd, mode=mode, tagsstring=params)
    elif cmd == 'subject':
        return commandfactory(cmd, mode=mode, key='Subject', value=params)
    elif cmd == 'shellescape':
        return commandfactory(cmd, mode=mode, commandstring=params)
    elif cmd == 'to':
        return commandfactory(cmd, mode=mode, key='To', value=params)
    elif cmd == 'toggletag':
        return commandfactory(cmd, mode=mode, tag=params)
    elif cmd == 'fold':
        return commandfactory(cmd, mode=mode, all=(params=='all'))
    elif cmd == 'unfold':
        return commandfactory(cmd, mode=mode, all=(params=='all'))
    elif cmd == 'edit':
        filepath = os.path.expanduser(params)
        if os.path.isfile(filepath):
            return commandfactory(cmd, mode=mode, path=filepath)

    elif not params and cmd in ['exit', 'flush', 'pyshell', 'taglist', 'close',
                                'compose', 'openfocussed', 'closefocussed',
                                'bnext', 'bprevious', 'retag', 'refresh',
                                'bufferlist', 'refineprompt', 'reply',
                                'forward', 'groupreply', 'bounce', 'openthread',
                                'send', 'reedit', 'select', 'retagprompt']:
        return commandfactory(cmd, mode=mode)
    else:
        return None