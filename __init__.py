#!/usr/bin/env python
from __future__ import (unicode_literals, division, absolute_import, print_function)

__license__ = 'GPL v3'
__copyright__ = '2020, Mick Kirkegaard (mickkn@gmail.com)'
__docformat__ = 'restructuredtext el'

import socket
import time
import datetime
import json
try:
    from queue import Empty, Queue
except ImportError:
    from Queue import Empty, Queue
from six import text_type as unicode
from html5_parser import parse
from lxml.html import tostring
from threading import Thread
from calibre.ebooks.metadata.sources.base import Source
from calibre.ebooks.metadata.book.base import Metadata
from calibre.library.comments import sanitize_comments_html

# This is a metadata plugin for Calibre. It has been made and tested on Calibre 5.4.2
# Most of the stuff is taken from the Goodreads plugin and Biblionet plugin.
# I've just gathered everything in one __init__.py file.

class Mofibo(Source):
    name = 'Mofibo'
    description = ('Downloads Metadata and Covers from Mofibo.dk based on ISBN')
    supported_platforms = ['windows', 'osx', 'linux']
    author = 'Mick Kirkegaard'
    version = (1, 0, 0)
    minimum_calibre_version = (5, 0, 1)

    capabilities = frozenset(['identify', 'cover'])
    touched_fields = frozenset(['identifier:isbn', 'identifier:mofibo', 'title', 'authors', 'tags', 'comments', 'publisher', 'language', 'pubdate'])

    supports_gzip_transfer_encoding = True

    ID_NAME = 'mofibo'
    BASE_URL = 'https://mofibo.com/dk/da/soeg-'

    def get_book_url(self, identifiers):
        mofibo_id = identifiers.get(self.ID_NAME, None)
        if mofibo_id:
            return ('Mofibo', mofibo_id, self.url)

    def identify(self, log, result_queue, abort, title=None, authors=None, identifiers={}, timeout=30):
        '''
        Redefined identity() function
        '''
        # Create matches list
        matches = []

        # Initialize browser object
        br = self.browser
        
        # Add mofibo url to matches if present
        mofibo = identifiers.get('mofibo', None)
        if mofibo:
            print("    Found comicwiki %s" % (mofibo))
            matches.append(mofibo)

        # Get ISBN number and report
        isbn = identifiers.get('isbn', None)
        if isbn:
            print("    Found isbn %s" % (isbn))
            search = ('%s%s' % (Mofibo.BASE_URL, isbn))
        
            mofibo_raw = br.open_novisit(search, timeout=30).read().strip()
            mofibo_root = parse(mofibo_raw)
            mofibo_nodes = mofibo_root.xpath('(//div[@class="gridCover"])//a/@href')
            log.info(mofibo_nodes)
            for url in mofibo_nodes[:5]:
                matches.append("https://mofibo.com" + url)
        
        
        # Return if no ISBN
        if abort.is_set():
            return

        # Report the matches
        log.info("    Matches are: ", matches)

        # Setup worker thread
        workers = [Worker(url, result_queue, br, log, i, self) for i, url in enumerate(matches)]

        # Start working
        for w in workers:
            w.start()
            # Delay a little for every worker
            time.sleep(0.1)

        while not abort.is_set():
            a_worker_is_alive = False
            for w in workers:
                w.join(0.2)
                if abort.is_set():
                    break
                if w.is_alive():
                    a_worker_is_alive = True
            if not a_worker_is_alive:
                break

        return None

    def get_cached_cover_url(self, identifiers):
        '''
        Redefined get_cached_cover_url() function
        Just fetch cached the cover url based on isbn, we don't
        use a saxo id in this plugin yet.
        '''
        isbn = identifiers.get('isbn', None)
        url = self.cached_identifier_to_cover_url(isbn)
        return url

    def download_cover(self, log, result_queue, abort, title=None, authors=None, identifiers={}, timeout=30):
        '''
        Redefined get_cached_cover_url() function
        Stolen from Goodreads.
        '''
        cached_url = self.get_cached_cover_url(identifiers)
        if cached_url is None:
            log.info('No cached cover found, running identify')
            rq = Queue()
            self.identify(log, rq, abort, title=title, authors=authors,
                          identifiers=identifiers)
            if abort.is_set():
                return
            results = []
            while True:
                try:
                    results.append(rq.get_nowait())
                except Empty:
                    break
            results.sort(key=self.identify_results_keygen(
                title=title, authors=authors, identifiers=identifiers))
            for mi in results:
                cached_url = self.get_cached_cover_url(mi.identifiers)
                if cached_url is not None:
                    break
        if cached_url is None:
            log.info('No cover found')
            return

        if abort.is_set():
            return
        br = self.browser
        log('    Downloading cover from:', cached_url)
        try:
            cdata = br.open_novisit(cached_url, timeout=timeout).read()
            result_queue.put((self, cdata))
        except:
            log.exception('Failed to download cover from:', cached_url)

def parse_comments(root):
    '''
    Function for parsing comments and clean them up a little
    Re-written script from the Goodreads script
    '''
    # Look for description
    description_node = root.xpath('(//div[@class="product-page-block"]//p)[1]')
    if description_node:
        desc = description_node[0] if len(description_node) == 1 else description_node[1]
        less_link = desc.xpath('a[@class="actionLinkLite"]')
        if less_link is not None and len(less_link):
            desc.remove(less_link[0])
        comments = tostring(desc, method='html', encoding=unicode).strip()
        while comments.find('  ') >= 0:
            comments = comments.replace('  ', ' ')
        if "Fil størrelse:" in comments:
            comments = comments.replace(comments.split(".")[-1], "</p>")
        comments = sanitize_comments_html(comments)
        return comments

class Worker(Thread):  # Get details
    '''
    Get book details from Saxos book page in a separate thread
    '''
    def __init__(self, url, result_queue, browser, log, relevance, plugin, timeout=20):
        Thread.__init__(self)
        self.title = None
        self.isbn = None
        self.daemon = True
        self.url = url
        self.result_queue = result_queue
        self.log = log
        self.language = None
        self.timeout = timeout
        self.relevance = relevance
        self.plugin = plugin
        self.browser = browser.clone_browser()
        self.cover_url = None
        self.authors = []
        self.comments = None
        self.pubdate = None
        self.tags = None

        # Mapping language to something calibre understand.
        lm = {
            'eng': ('English', 'Engelsk'),
            'dan': ('Danish', 'Dansk'),
        }
        self.lang_map = {}
        for code, names in lm.items():
            for name in names:
                self.lang_map[name] = code

    def run(self):
        self.log.info("    Worker.run: self: ", self)
        try:
            self.get_details()
        except:
            self.log.exception('get_details() failed for url: %r' % self.url)

    def get_details(self):
        '''
        The get_details() function for stripping the website for all information
        '''
        self.log.info("    Worker.get_details:")
        self.log.info("        self:     ", self)
        self.log.info("        self.url: ", self.url)

        # Parse the html code from the website
        try:
            raw = self.browser.open_novisit(self.url, timeout=self.timeout).read().strip()
        # Do some error handling if it fails to read data
        except Exception as e:
            if callable(getattr(e, 'getcode', None)) and e.getcode() == 404:
                self.log.error('URL malformed: %r' % self.url)
                return
            attr = getattr(e, 'args', [None])
            attr = attr if attr else [None]
            if isinstance(attr[0], socket.timeout):
                msg = 'Bookmeta for saxo timed out. Try again later.'
                self.log.error(msg)
            else:
                msg = 'Failed to make details query: %r' % self.url
                self.log.exception(msg)
            return

        # Do some error handling if the html code returned 404
        if "<title>404 - " == raw:
            self.log.error('URL malformed: %r' % self.url)
            return

        # Clean the html data a little
        try:
            root = parse(raw)
        except:
            self.log.error("Error cleaning HTML")
            return

        # Get the title of the book
        try:
            title_node = root.xpath('//span[@itemprop="name"]')
            self.title = title_node[0].text
        except:
            self.log.exception('Error parsing title for url: %r' % self.url)

        # Get the author of the book
        try:
            author_node = root.xpath('//span[@class="expandAuthorName"]')
            author_node = author_node[0].text.split(",")
            for name in author_node:
                self.authors.append(name)
        except:
            self.log.exception('Error parsing authors for url: %r' % self.url)
            self.authors = None

        # Some books have ratings, let's use them.
        try:
            self.rating = 0.0
        except:
            self.log.exception('Error parsing rating for url: %r' % self.url)
            self.rating = 0.0

        # Get the ISBN number from the site
        try:
            isbn_node = root.xpath('//div[@class="eBookContainer"]/b/span[@itemprop="identifier"]')
            self.isbn = isbn_node[0].text.replace("ISBN: ", "").strip()
        except:
            self.log.exception('Error parsing isbn for url: %r' % self.url)
            self.isbn = None

        # Get the comments/blurb for the book
        try:
            comment_node = root.xpath('//meta[@name="description"]/@content')
            self.comments = comment_node[0]
        except:
            self.log.exception('Error parsing comments for url: %r' % self.url)
            self.comments = None

        # Parse the cover url for downloading the cover.
        try:
            cover_node = root.xpath('//div[@class="bookDetailCoverCover"]/img/@src')
            self.cover_url = "https://mofibo.com" + cover_node[0]
            self.log.info('    Parsed URL for cover: %r' % self.cover_url)
            self.plugin.cache_identifier_to_cover_url(self.isbn, self.cover_url)
        except:
            self.log.exception('Error parsing cover for url: %r' % self.url)
            self.has_cover = bool(self.cover_url)

        # Get the publisher name
        try:
            publisher_node = root.xpath('//div[@class="eBookContainer"]/b/span/a[@itemprop="brand"]')
            self.publisher = publisher_node[0].text
        except:
            self.log.exception('Error parsing publisher for url: %r' % self.url)

        # Get the language of the book. Only english and danish are supported tho
        try:
            language_node = root.xpath('//b[@class="expanderLanguage"]')
            language = language_node[0].text.strip().replace("Sprog:", "").replace(" ", "")
            language = self.lang_map.get(language, None)
            self.language = language
        except:
            self.log.exception('Error parsing language for url: %r' % self.url)

        # Get the publisher date
        try:
            pubdate_node = root.xpath('//div[@class="eBookContainer"]/b[contains(text(),"Udgivet")]')
            date_str = pubdate_node[0].text.replace("Udgivet:", "").strip()
            format_str = '%Y-%m-%d' # The format
            self.pubdate = datetime.datetime.strptime(date_str, format_str)
        except:
            self.log.exception('Error parsing published date for url: %r' % self.url)

        # Get the tags
        try:
            tags_node = root.xpath('//span[@itemprop="category"]')
            self.tags = tags_node[0].text.split()
        except:
            self.log.exception('Error parsing published date for url: %r' % self.url)

        # Setup the metadata
        meta_data = Metadata(self.title, self.authors)
        meta_data.set_identifier('isbn', self.isbn)
        meta_data.set_identifier('mofibo', self.url)

        # Set rating
        """
        if self.rating:
            try:
                meta_data.rating = self.rating
            except:
                self.log.exception('Error loading rating')
        """
        # Set ISBN
        if self.isbn:
            try:
                meta_data.isbn = self.isbn
            except:
                self.log.exception('Error loading ISBN')
        # Set relevance
        if self.relevance:
            try:
                meta_data.source_relevance = self.relevance
            except:
                self.log.exception('Error loading relevance')
        # Set cover url
        if self.cover_url:
            try:
                meta_data.cover_url = self.cover_url
            except:
                self.log.exception('Error loading cover_url')
        # Set publisher
        if self.publisher:
            try:
                meta_data.publisher = self.publisher
            except:
                self.log.exception('Error loading publisher')
        # Set language
        if self.language:
            try:
                meta_data.language = self.language
            except:
                self.log.exception('Error loading language')
        # Set comments/blurb
        if self.comments:
            try:
                meta_data.comments = self.comments
            except:
                self.log.exception("Error loading comments")
        # Set publisher data
        if self.pubdate:
            try:
                meta_data.pubdate = self.pubdate
            except:
                self.log.exception('Error loading pubdate')
        # Set tags data
        if self.tags:
            try:
                meta_data.tags = self.tags
            except:
                self.log.exception('Error loading tags')

        # Put meta data
        self.plugin.clean_downloaded_metadata(meta_data)
        self.result_queue.put(meta_data)

if __name__ == '__main__':  # tests
    # To run these test use:
    # calibre-customize -b . ; calibre-debug -e __init__.py
    from calibre.ebooks.metadata.sources.test import (test_identify_plugin, title_test, authors_test)

    tests = [(  # A book with an ISBN
                {
                'identifiers': {'isbn': '9788740065756'},
                'title': 'Casper', 
                'authors': ['Martin Kongstad']
                },[
                    title_test('Casper', exact=True),
                    authors_test(['Martin Kongstad'])]
            ), 
            (   # A book with two Authors
                {
                'identifiers': {'isbn': '9788771761306'},
                'title': 'Elverfolket- Ulverytterne og solfolket', 
                'authors': ['Richard Pini & Wendy']
                },[
                    title_test('Elverfolket- Ulverytterne og solfolket', exact=True),
                    authors_test(['Richard Pini', 'Wendy'])]
            )
            ]

    test_identify_plugin(Mofibo.name, tests)
