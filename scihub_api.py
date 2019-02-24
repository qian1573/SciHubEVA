#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import os
import re
import tempfile
import threading
import requests

from enum import Enum, unique
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from urllib.parse import urlparse
from lxml import etree
from pdfminer.pdfparser import PDFParser
from pdfminer.pdfdocument import PDFDocument
from PySide2.QtCore import QObject

from scihub_conf import SciHubConf
from scihub_utils import make_pdf_metadata_str, pdf_metadata_moddate_to_year


@unique
class SciHubRampageType(Enum):
    # Input from main window
    INPUT = 0

    # Response of fetching PDF (if response is not PDF, but a HTML with captcha)
    PDF_CAPTCHA_RESPONSE = 1


@unique
class SciHubError(Enum):
    # Unknown error
    UNKNOWN = 0

    # Cannot find a valid iframe when fetching PDF URL
    NO_VALID_IFRAME = 1

    # Cannot download automatically due to captcha
    BLOCKED_BY_CAPTCHA = 2

    # Wrong captcha
    WRONG_CAPTCHA = 3


class SciHubAPI(QObject, threading.Thread):
    def __init__(self, query, callback=None, rampage_type=None, conf=None, log=None, **kwargs):
        QObject.__init__(self)
        threading.Thread.__init__(self)

        self._query = query
        self._callback = callback
        self._rampage_type = rampage_type

        # Captcha answer, used only when rampage_type == SciHubRampageType.PDF_CAPTCHA_RESPONSE
        if 'captcha_answer' in kwargs:
            self._captcha_answer = kwargs['captcha_answer']

        if conf:
            self._conf = conf
        else:
            self._conf = SciHubConf('SciHubEVA.conf')

        if log:
            self.log = log

        self._sess = requests.Session()
        self._sess.headers = json.loads(self._conf.get('network', 'session_header'))

        retry_times = self._conf.getint('network', 'retry_times')
        retry = Retry(total=retry_times, read=retry_times, connect=retry_times)
        adapter = HTTPAdapter(max_retries=retry)
        self._sess.mount('http://', adapter)
        self._sess.mount('https://', adapter)

        self._set_http_proxy()

        self._doi_pattern = r'\b(10[.][0-9]{4,}(?:[.][0-9]+)*/(?:(?!["&\'])\S)+)\b'
        self._illegal_filename_pattern = r'[\/\\\:\*\?\"\<\>\|]'

    def log(self, message, level=None):
        if level:
            log_formatter = '[{level}] - {message}'
        else:
            log_formatter = '{message}'

        print(log_formatter.format(level=level, message=message))

    def _set_http_proxy(self):
        if self._conf.getboolean('proxy', 'enabled'):
            proxy_type = self._conf.get('proxy', 'type')
            proxy_host = self._conf.get('proxy', 'host')
            proxy_port = self._conf.get('proxy', 'port')
            proxy_username = self._conf.get('proxy', 'username')
            proxy_password = self._conf.get('proxy', 'password')

            proxy = proxy_type + '://'

            if proxy_username and proxy_username != '':
                proxy += proxy_username

            if proxy_password and proxy_password != '':
                proxy += proxy_password

            if proxy_username and proxy_username != '':
                proxy += '@'

            proxy += proxy_host

            if proxy_port and proxy_port != '':
                proxy += ':' + proxy_port

            self._sess.proxies = {'http': proxy, 'https': proxy}

    @staticmethod
    def get_pdf_metadata(pdf):
        """Get PDF metadata with PDF content

        Args:
            pdf: PDF content (in bytes)

        Returns:
            metadata: PDF metadata dictionary

        """

        temp_pdf_file = tempfile.TemporaryFile()
        temp_pdf_file.write(pdf)

        metadata = {'author': 'UNKNOWN_AUTHOR',
                    'title': 'UNKNOWN_TITLE',
                    'year': 'UNKNOWN_YEAR'}

        pdf_parser = PDFParser(temp_pdf_file)
        pdf_doc = PDFDocument(pdf_parser)
        pdf_metadata = pdf_doc.info[0]

        author = make_pdf_metadata_str(pdf_metadata.get('Author', ''))
        if author and author != '':
            metadata['author'] = author

        title = make_pdf_metadata_str(pdf_metadata.get('Title', ''))
        if title and title != '':
            metadata['title'] = title

        year = pdf_metadata_moddate_to_year(make_pdf_metadata_str(pdf_metadata.get('ModDate', '')))
        if year and year != '':
            metadata['year'] = year

        temp_pdf_file.close()

        return metadata

    def guess_query_type(self, query):
        """Guess query type

        Args:
            query: Query

        Returns:
            query_type: Query type

        """

        if query.startswith('http') or query.startswith('https'):
            if query.endswith('pdf'):
                query_type = 'pdf'
            else:
                query_type = 'url'
        elif query.isdigit():
            query_type = 'pmid'
        elif query.startswith('doi:') or re.match(self._doi_pattern, query):
            query_type = 'doi'
        else:
            query_type = 'string'

        self.log(self.tr('Query type: ') + query_type.upper(), 'INFO')

        return query_type

    @staticmethod
    def get_captcha_info(pdf_captcha_response):
        """Get captcha information with PDF captcha response

        Args:
            pdf_captcha_response: PDF captcha response

        Returns:
            captcha_id: Captcha ID
            captcha_img_url: Captcha image URL

        """

        captcha_id, captcha_img_url = None, None

        html = etree.HTML(pdf_captcha_response.content)
        imgs = html.xpath('//img[@id="captcha"]')
        ids = html.xpath('//input[@name="id"]')

        if len(imgs) > 0 and len(ids) > 0:
            captcha_id = ids[0].attrib['value']
            captcha_img_src = imgs[0].attrib['src']
            scheme, netloc, *_ = urlparse(pdf_captcha_response.url, scheme='http')
            captcha_img_url = scheme + '://' + netloc + captcha_img_src

        return captcha_id, captcha_img_url

    def fetch_pdf_with_captcha(self, pdf_captcha_response):
        """Fetch PDF with captcha

        Args:
            pdf_captcha_response: PDF captcha response

        Returns:
            pdf: PDF content (in bytes)
            err: Error

        """

        pdf, err = None, None

        captcha_id, _ = self.get_captcha_info(pdf_captcha_response)

        pdf_response = self._sess.post(
            pdf_captcha_response.url, data={'answer': self._captcha_answer, 'id': captcha_id}, verify=False,
            timeout=self._conf.getfloat('network', 'timeout') / 1000.0)

        if pdf_response.headers['Content-Type'] == 'application/pdf':
            self.log(self.tr('Angel [CAPTCHA] down!'), 'INFO')
            pdf = pdf_response.content
        else:
            err = SciHubError.WRONG_CAPTCHA

        return pdf, err

    def fetch_pdf(self, pdf_url):
        """ Fetch PDF with PDF URL

        Args:
            pdf_url: PDF URL

        Returns:
            pdf: PDF (in bytes) or PDF captcha response (when downloading is blocked by captcha)
            err: Error

        """

        self.log(self.tr('Fetching PDF ...'), 'INFO')

        pdf, err = None, None

        pdf_response = self._sess.get(
            pdf_url, verify=False,
            timeout=self._conf.getfloat('network', 'timeout') / 1000.0)

        if pdf_response.headers['Content-Type'] == 'application/pdf':
            pdf = pdf_response.content
        elif pdf_response.headers['Content-Type'].startswith('text/html'):
            self.log(self.tr('Angel [CAPTCHA] is coming!'), 'WARNING')
            err = SciHubError.BLOCKED_BY_CAPTCHA
            pdf = pdf_response
        else:
            self.log(self.tr('Unknown PDF Content-Type!'), 'ERROR')

        return pdf, err

    def fetch_pdf_url(self, query):
        """Fetch PDF URL with query

        Args:
            query: Query

        Returns:
            pdf_url: PDF URL
            err: Error

        """

        scihub_url = self._conf.get('network', 'scihub_url')
        self.log(self.tr('Using Sci-Hub URL: ') +
                 '<a href="{scihub_url}">{scihub_url}</a>'.format(scihub_url=scihub_url), 'INFO')

        query_type = self.guess_query_type(query)
        pdf_url = query
        err = None

        if query_type != 'pdf':
            try:
                self.log(self.tr('Fetching PDF URL ...'), 'INFO')

                pdf_url_response = self._sess.post(
                    scihub_url, data={'request': query}, verify=False,
                    timeout=self._conf.getfloat('network', 'timeout') / 1000.0)

                html = etree.HTML(pdf_url_response.content)
                iframes = html.xpath('//iframe[@id="pdf"]')

                if len(iframes) > 0:
                    pdf_url = urlparse(iframes[0].attrib['src'], scheme='http').geturl()
                    pdf_url_html = '<a href="{pdf_url}">{pdf_url}</a>'.format(pdf_url=pdf_url)

                    self.log(self.tr('Got PDF URL: ') + pdf_url_html, 'INFO')
                else:
                    err = SciHubError.NO_VALID_IFRAME
                    request_url = '{scihub_url}/{query}'.format(scihub_url=scihub_url, query=query)
                    request_url_html = '<a href="{request_url}">{request_url}</a>'.format(request_url=request_url)
                    response_url = pdf_url_response.url
                    response_url_html = '<a href="{response_url}">{response_url}</a>'.format(response_url=response_url)

                    self.log(self.tr('Failed to get PDF URL!'), 'ERROR')
                    self.log(self.tr('No valid &lt;iframe&gt;!'), 'ERROR')
                    self.log(self.tr('You may need handle it manually.'), 'INFO')
                    self.log(self.tr('Request URL: ') + request_url_html, 'INFO')
                    self.log(self.tr('Response URL: ') + response_url_html, 'INFO')
            except Exception as e:
                err = SciHubError.UNKNOWN

                self.log(self.tr('Failed to get PDF!'), 'ERROR')
                self.log(str(e), 'ERROR')

        return pdf_url, err

    def save_pdf(self, pdf, filename):
        """Save pdf to locaal

        Args:
            pdf: PDF content (in bytes)
            filename: PDF filename

        """

        pdf_name_formatter = self._conf.get('common', 'filename_prefix_format') + '_' + filename
        pdf_metadata = self.get_pdf_metadata(pdf)
        pdf_name = pdf_name_formatter.format(**pdf_metadata)
        pdf_path = os.path.join(self._conf.get('common', 'save_to_dir'), pdf_name)

        with open(pdf_path, 'wb') as fp:
            fp.write(pdf)

        self.log(self.tr('Saved PDF as: ') + pdf_name, 'INFO')

    def rampage(self, query, rampage_type):
        """Main process of downloading PDF

        Args:
            query: Query (input, response of fetching PDF, ...)
            rampage_type: Rampage type

        Returns:
            res: Result of rampage, maybe used for next steps
            err: Error of rampage

            e.g. (None, None), (pdf_captcha_response, SciHubError.BLOCKED_BY_CAPTCHA), ...

        """

        if rampage_type == SciHubRampageType.INPUT:
            # Query is user input

            self.log('')
            self.log(self.tr('Dealing with query: ') + query, 'INFO')

            # Fetch PDF URL
            pdf_url, err = self.fetch_pdf_url(query)
            if err is not None:
                return None, err

            # Fetch PDF
            pdf, err = self.fetch_pdf(pdf_url)
            if err == SciHubError.BLOCKED_BY_CAPTCHA:
                return pdf, err
            elif err is not None:
                return None, err

            # Save PDF
            filename = urlparse(pdf_url).path[1:].split('/')[-1]
            self.save_pdf(pdf, filename)
        elif rampage_type == SciHubRampageType.PDF_CAPTCHA_RESPONSE:
            # Query is PDF captcha response (with answer)

            # Fetch PDF with Captcha
            pdf, err = self.fetch_pdf_with_captcha(query)
            if err == SciHubError.WRONG_CAPTCHA:
                self.log(self.tr('Wrong captcha, failed to kill Angel [CAPTCHA]!'), 'ERROR')
                return None, err

            # Save PDF
            filename = urlparse(query.url).path[1:].split('/')[-1]
            self.save_pdf(pdf, filename)

        return None, None

    def run(self):
        res, err = self.rampage(self._query, self._rampage_type)
        self._callback(res, err)
