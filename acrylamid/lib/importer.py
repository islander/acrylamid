#!/usr/bin/env python
# -*- encoding: utf-8 -*-
#
# Copyright 2012 posativ <info@posativ.org>. All rights reserved.
# License: BSD Style, 2 clauses. see acrylamid/__init__.py

import os
import re
import tempfile
import getpass

from base64 import b64encode
from datetime import datetime
from urllib2 import urlopen, Request, HTTPError
from urlparse import urlsplit

from xml.etree import ElementTree
from htmlentitydefs import name2codepoint

from email.utils import parsedate_tz, mktime_tz
from os.path import join, dirname, isfile

from acrylamid import log
from acrylamid.utils import FileEntry, event, escape, system
from acrylamid.errors import AcrylamidException


class InvalidSource(Exception):
    pass


def unescape(s):
    return re.sub('&(%s);' % '|'.join(name2codepoint),
            lambda m: unichr(name2codepoint[m.group(1)]), s)


def convert(data, fmt='markdown'):
    """Reconversion of HTML to Markdown or reStructuredText.  Defaults to Markdown,
    but can be in fact every format pandoc supports. If pandoc is not available, try
    some specific conversion tools like html2text and html2rest.

    :param html: raw content to convert to
    :param html: format to reconvert to"""

    if fmt in ('Markdown', 'markdown', 'mkdown', 'md', 'mkd'):
        cmds = ['html2text']
        fmt = 'markdown'
    elif fmt in ('rst', 'restructuredtext', 'rest', 'reStructuredText'):
        cmds = ['html2rest']
        fmt = 'rst'
    else:
        cmds = []

    cmds.insert(0, ['pandoc', '--normalize', '-f', 'html', '-t', fmt, '--strict'])

    if fmt == 'html':
        return data, 'html'

    #  - item.find(foo).text returns None if no CDATA
    #  - pandoc waits for input if a zero-length string is given
    if data is None or data is '':
        return '', fmt

    for cmd in cmds:
        try:
            return system(cmd, stdin=str(data.decode('utf-8'))), fmt.lower()
        except AcrylamidException as e:
            log.warn(e.message)
        except OSError:
            pass
    else:
        return data, 'html'


def rss20(xml):

    def parse_date_time(stamp):
        ts = parsedate_tz(stamp)
        ts = mktime_tz(ts)
        return datetime.fromtimestamp(ts)

    try:
        tree = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        raise InvalidSource('no well-formed XML')
    if tree.tag != 'rss' or tree.attrib.get('version') == '2.0':
        raise InvalidSource('no RSS 2.0 feed')

    # --- site settings --- #
    defaults = {}
    channel = tree.getchildren()[0]

    for k, v in {'title': 'sitename', 'link': 'www_root', 'language': 'lang'}.iteritems():
        try:
            defaults[v] = channel.find(k).text
        except AttributeError:
            pass

    yield defaults

    # --- individual posts --- #
    for item in channel.findall('item'):

        entry = {}
        for k, v in {'title': 'title', 'date': 'pubDate', 'link': 'link',
                     'content': 'description'}.iteritems():
            try:
                entry[k] = item.find(v).text if k != 'content' \
                                             else unescape(item.find(v).text)
            except (AttributeError, TypeError):
                pass

        if filter(lambda k: not k in entry, ['title', 'date', 'link', 'content']):
            raise AcrylamidException('invalid RSS 2.0 feed: provide at least title, ' \
                                     + 'link, content and pubDate!')

        yield {'title': entry['title'],
               'content': entry['content'],
               'date': parse_date_time(entry['date']),
               'link': entry['link']}


def atom(xml):

    def parse_date_time(stamp):
        ts = parsedate_tz(stamp)
        ts = mktime_tz(ts)
        return datetime.fromtimestamp(ts)

    try:
        tree = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        raise InvalidSource('no well-formed XML')


    if not tree.tag.endswith('/2005/Atom}feed'):
        raise InvalidSource('no Atom feed')

    # --- site settings --- #
    ns = '{http://www.w3.org/2005/Atom}'  # etree Y U have stupid namespace handling?
    defaults = {}

    defaults['title'] = tree.find(ns + 'title').text
    defaults['www_root'] = tree.find(ns + 'id').text
    defaults['author'] = tree.find(ns + 'author').find(ns + 'name').text

    yield defaults

    # --- individual posts --- #
    for item in tree.findall(ns + 'entry'):
        entry = {}

        try:
            entry['title'] = item.find(ns + 'title').text
            entry['date'] = item.find(ns + 'updated').text
            entry['link'] = item.find(ns + 'link').text
            entry['content'] = item.find(ns + 'content').text
        except (AttributeError, TypeError):
            pass

        if item.find(ns + 'content').get('type', 'text') == 'html':
            entry['content'] = unescape(entry['content'])

        if filter(lambda k: not k in entry, ['title', 'date', 'link', 'content']):
            raise AcrylamidException('invalid Atom feed: provide at least title, '
                                     + 'link, content and updated!')

        yield {'title': entry['title'],
               'content': entry['content'],
               'date': datetime.strptime(entry['date'], "%Y-%m-%dT%H:%M:%SZ"),
               'link': entry['link']}


def fetch(url, auth=None):
    """Fetch URL, optional with HTTP Basic Authentication."""

    if not (url.startswith('http://') or url.startswith('https://')):
        try:
            with open(url, 'r') as fp:
                content = fp.read()
            return content
        except OSError as e:
            raise AcrylamidException(e.message)

    req = Request(url)
    if auth:
        req.add_header('Authorization', 'Basic ' + b64encode(auth))

    try:
        r = urlopen(req)
    except HTTPError as e:
        raise AcrylamidException(e.msg)

    if r.getcode() == 401:
        user = raw_input('Username: ')
        passwd = getpass.getpass()
        fetch(url, user + ':' + passwd)
    elif r.getcode() == 200:
        return r.read()

    raise AcrylamidException('invalid status code %i, aborting.' % r.getcode())


def parse(content):

    failed = []
    for method in (rss20, atom):
        try:
            res =  method(content)
            return res.next(), res
        # except ValueError as e:
        #     raise AcrylamidException(e.message)
        except InvalidSource as e:
            failed.append(str(e))
    else:
        raise AcrylamidException('unable to parse source, %s' % '; '.join(failed))


def build(conf, env, defaults, items, fmt, keep=False):

    def create(title, date, content, permalink=None):

        fd, tmp = tempfile.mkstemp(suffix='.txt')
        title = escape(title)

        with os.fdopen(fd, 'wb') as f:
            f.write('---\n')
            f.write('title: %s\n' % title)
            f.write('date: %s\n' % date.strftime(conf['date_format']))
            f.write('filter: [%s, ]\n' % fmt)
            if permalink:
                f.write('permalink: %s\n' % permalink)
            f.write('---\n\n')
            f.write(content[0])

        entry = FileEntry(tmp, conf)
        p = join(conf['entries_dir'], dirname(entry.permalink)[1:])

        try:
            os.makedirs(p.rsplit('/', 1)[0])
        except OSError:
            pass

        filepath = p + '.txt'
        if isfile(filepath):
            raise AcrylamidException('Entry already exists %r' % filepath)
        os.rename(tmp, filepath)
        event.create(filepath)

    for item in items:

        if keep:
            m = urlsplit(item['link'])
            permalink = m.path if m.path != '/' else None

        create(item['title'], item['date'], convert(item.get('content', ''), fmt),
               permalink=permalink if keep else None)


__all__ = ['fetch', 'parse', 'build']
