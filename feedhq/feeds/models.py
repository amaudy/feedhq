import bleach
import datetime
import feedparser
import json
import logging
import lxml
import magic
import oauth2 as oauth
import urllib
import urlparse
import random
import requests
import socket

from django.db import models
from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.urlresolvers import reverse
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _

from django_push.subscriber.signals import updated

from .tasks import update_feed, update_unique_feed
from .utils import FeedUpdater, FAVICON_FETCHER, USER_AGENT
from ..storage import OverwritingStorage
from ..tasks import enqueue

logger = logging.getLogger('feedupdater')

feedparser.PARSE_MICROFORMATS = False
feedparser.SANITIZE_HTML = False

COLORS = (
    ('red', _('Red')),
    ('dark-red', _('Dark Red')),
    ('pale-green', _('Pale Green')),
    ('green', _('Green')),
    ('army-green', _('Army Green')),
    ('pale-blue', _('Pale Blue')),
    ('blue', _('Blue')),
    ('dark-blue', _('Dark Blue')),
    ('orange', _('Orange')),
    ('dark-orange', _('Dark Orange')),
    ('black', _('Black')),
    ('gray', _('Gray')),
)


def random_color():
    return random.choice(COLORS)[0]


DURATIONS = (
    ('1day', _('One day')),
    ('2days', _('Two days')),
    ('1week', _('One week')),
    ('1month', _('One month')),
    ('1year', _('One year')),
)


TIMEDELTAS = {
    '1day': datetime.timedelta(days=1),
    '2days': datetime.timedelta(days=2),
    '1week': datetime.timedelta(weeks=1),
    '1month': datetime.timedelta(days=30),
    '1year': datetime.timedelta(days=365),
    #'never': None, # Implicit
}


class CategoryManager(models.Manager):

    def with_unread_counts(self):
        return self.values('id', 'name', 'slug', 'color').annotate(
            unread_count=models.Sum('feeds__unread_count'))


class Category(models.Model):
    """Used to sort our feeds"""
    name = models.CharField(_('Name'), max_length=50)
    slug = models.SlugField(_('Slug'), db_index=True)
    user = models.ForeignKey(User, verbose_name=_('User'),
                             related_name='categories')
    # Some day there will be drag'n'drop ordering
    order = models.PositiveIntegerField(blank=True, null=True)

    # Categories have nice cute colors
    color = models.CharField(_('Color'), max_length=50, choices=COLORS,
                             default=random_color)

    # We delete the old entries after a certain while
    delete_after = models.CharField(
        _('Delete after'), max_length=50, choices=DURATIONS, default='1month',
        help_text=_("Period of time after which entries are deleted, whether "
                    "they've been read or not."),
    )

    objects = CategoryManager()

    def __unicode__(self):
        return u'%s' % self.name

    class Meta:
        ordering = ('order', 'name', 'id')
        verbose_name_plural = 'categories'

    def get_absolute_url(self):
        return reverse('feeds:category', args=[self.slug])


class UniqueFeedManager(models.Manager):
    def update_feed(self, url, use_etags=True):
        obj, created = self.get_or_create(url=url)
        save = True

        if not created and use_etags:
            if not obj.should_update():
                logger.debug("Last update too recent, skipping %s" % obj.url)
                return
        obj.last_update = timezone.now()

        if obj.muted:
            logger.debug("%s is muted" % obj.url)
            return

        feeds = Feed.objects.filter(url=url)

        obj.subscribers = feeds.count()

        if obj.subscribers == 0:
            logger.debug("%s has no subscribers, deleting" % obj.url)
            obj.delete()
            return

        if obj.subscribers == 1:
            subscribers = '1 subscriber'
        else:
            subscribers = '%s subscribers' % obj.subscribers

        headers = {
            'User-Agent': USER_AGENT % subscribers,
            'Accept': feedparser.ACCEPT_HEADER,
        }

        if use_etags:
            if obj.modified:
                headers['If-Modified-Since'] = obj.modified
            if obj.etag:
                headers['If-None-Match'] = obj.etag

        if settings.TESTS:
            # Make sure requests.get is properly mocked during tests
            if str(type(requests.get)) != "<class 'mock.MagicMock'>":
                raise ValueError("Not Mocked")

        start = datetime.datetime.now()
        try:
            response = requests.get(url, headers=headers,
                                    timeout=obj.request_timeout)
        except (requests.RequestException, socket.timeout) as e:
            logger.debug("Error fetching %s, %s" % (obj.url, str(e)))
            if obj.backoff_factor == obj.MAX_BACKOFF - 1:
                logger.info(
                    "%s reached max backoff period (timeout)" % obj.url
                )
            obj.backoff()
            obj.error = 'timeout'
            if save:
                obj.save()
            return

        elapsed = (datetime.datetime.now() - start).seconds

        ctype = response.headers.get('Content-Type', None)
        if (response.history and
            obj.url != response.url and ctype is not None and (
                ctype.startswith('application') or
                ctype.startswith('text/xml') or
                ctype.startswith('text/rss'))):
            redirection = None
            for index, redirect in enumerate(response.history):
                if redirect.status_code != 301:
                    break
                # Actual redirection is next request's url
                try:
                    redirection = response.history[index + 1].url
                except IndexError:  # next request is final request
                    redirection = response.url

            if redirection is not None and redirection != obj.url:
                logger.debug("%s moved to %s" % (obj.url, redirection))
                Feed.objects.filter(url=obj.url).update(url=redirection)
                if self.filter(url=redirection).exists():
                    obj.delete()
                    save = False
                else:
                    obj.url = redirection

        if response.status_code == 410:
            logger.info("Feed gone, %s" % obj.url)
            obj.muted = True
            obj.error = 'gone'
            obj.save()
            return

        elif response.status_code in [400, 401, 403, 404, 500, 502, 503]:
            if obj.backoff_factor == obj.MAX_BACKOFF - 1:
                logger.info("%s reached max backoff period (%s)" % (
                    obj.url, response.status_code,
                ))
            obj.backoff()
            obj.error = str(response.status_code)
            if save:
                obj.save()
            return

        elif response.status_code not in [200, 204, 304]:
            logger.debug("%s returned %s" % (obj.url, response.status_code))

        else:
            # Avoid going back to 1 directly if it isn't safe given the
            # actual response time.
            obj.backoff_factor = min(obj.backoff_factor,
                                     obj.safe_backoff(elapsed))
            obj.error = None

        if 'etag' in response.headers:
            obj.etag = response.headers['etag']

        if 'last-modified' in response.headers:
            obj.modified = response.headers['last-modified']

        if response.status_code == 304:
            logger.debug("Feed not modified, %s" % obj.url)
            if save:
                obj.save()
            return

        try:
            if not response.content:
                content = ' '  # chardet won't detect encoding on empty strings
            else:
                content = response.content
        except socket.timeout:
            logger.debug('%s timed out' % obj.url)
            return
        parsed = feedparser.parse(content)

        if 'link' in parsed.feed:
            obj.link = parsed.feed.link

        if 'title' in parsed.feed:
            obj.title = parsed.feed.title

        if 'links' in parsed.feed:
            for link in parsed.feed.links:
                if link.rel == 'hub':
                    obj.hub = link.href

        if save:
            obj.save()

        updater = FeedUpdater(parsed=parsed, feeds=feeds, hub=obj.hub)
        updater.update()


MUTE_CHOICES = (
    ('gone', 'Feed gone (410)'),
    ('timeout', 'Feed timed out'),
    ('400', 'HTTP 400'),
    ('401', 'HTTP 401'),
    ('403', 'HTTP 403'),
    ('404', 'HTTP 404'),
    ('500', 'HTTP 500'),
    ('502', 'HTTP 502'),
    ('503', 'HTTP 503'),
)


class UniqueFeed(models.Model):
    url = models.URLField(_('URL'), max_length=1023, unique=True)
    title = models.CharField(_('Title'), max_length=1023, blank=True)
    link = models.URLField(_('Link'), max_length=1023, blank=True)
    etag = models.CharField(_('Etag'), max_length=1023, null=True, blank=True)
    modified = models.CharField(_('Modified'), max_length=1023, null=True,
                                blank=True)
    subscribers = models.PositiveIntegerField(default=1, db_index=True)
    last_update = models.DateTimeField(_('Last update'), default=timezone.now,
                                       db_index=True)
    muted = models.BooleanField(_('Muted'), default=False, db_index=True)
    # Muted is only for 410, this is populated even when the feed is not
    # muted. It's more an indicator of the reason the backoff factor isn't 1.
    error = models.CharField(_('Error'), max_length=50, null=True, blank=True,
                             choices=MUTE_CHOICES, db_column='muted_reason')
    hub = models.URLField(_('Hub'), max_length=1023, null=True, blank=True)
    backoff_factor = models.PositiveIntegerField(_('Backoff factor'),
                                                 default=1)
    last_loop = models.DateTimeField(_('Last loop'), default=timezone.now,
                                     db_index=True)

    objects = UniqueFeedManager()

    MAX_BACKOFF = 10  # Approx. 24 hours

    def __unicode__(self):
        if self.title:
            return u'%s' % self.title
        return u'%s' % self.url

    def backoff(self):
        self.backoff_factor = min(self.MAX_BACKOFF, self.backoff_factor + 1)

    @property
    def task_timeout(self):
        return 20 * self.backoff_factor

    @property
    def request_timeout(self):
        return 10 * self.backoff_factor

    def safe_backoff(self, response_time):
        """
        Returns the backoff factor that should be used to keep the feed
        working given the last response time. Keep a margin. Backoff time
        shouldn't increase, this is only used to avoid returning back to 10s
        if the response took more than that.
        """
        return int((response_time * 1.2) / 10) + 1

    def should_update(self):
        # Exponential backoff: max backoff factor is 10, which is approx. 24
        # hours. This way we avoid muting and resurrecting feeds, failing
        # feeds stay at a backoff factor of 10.
        minutes = 45 * (self.backoff_factor ** 1.5)
        delay = datetime.timedelta(minutes=minutes)
        return self.last_update + delay < timezone.now()


class Feed(models.Model):
    """A URL and some extra stuff"""
    name = models.CharField(_('Name'), max_length=255)
    url = models.URLField(_('URL'), max_length=1023)
    category = models.ForeignKey(
        Category, verbose_name=_('Category'), related_name='feeds',
        help_text=_('<a href="/category/add/">Add a category</a>'),
    )
    # Mute a feed when we don't want the updates to show up in the timeline
    muted = models.BooleanField(_('Muted'), default=False,
                                help_text=_('Check this if you want to stop '
                                            'checking updates for this feed'))
    unread_count = models.PositiveIntegerField(_('Unread count'), default=0)
    favicon = models.ImageField(_('Favicon'), upload_to='favicons', null=True,
                                storage=OverwritingStorage())
    img_safe = models.BooleanField(_('Display images by default'),
                                   default=False)

    def __unicode__(self):
        return u'%s' % self.name

    class Meta:
        ordering = ('name',)

    def get_absolute_url(self):
        return reverse('feeds:feed', args=[self.id])

    def save(self, *args, **kwargs):
        update = self.pk is None
        super(Feed, self).save(*args, **kwargs)
        if update:
            enqueue(update_feed, args=[self.url], kwargs={'use_etags': False},
                    timeout=20, queue='high')
        enqueue(update_unique_feed, args=[self.url], timeout=20)

    @property
    def media_safe(self):
        return self.img_safe

    def favicon_img(self):
        if not self.favicon:
            return ''
        return '<img src="%s" width="16" height="16" />' % self.favicon.url
    favicon_img.allow_tags = True

    def get_treshold(self):
        """Returns the date after which the entries can be ignored / deleted"""
        del_after = self.category.delete_after

        if del_after == 'never':
            return None
        return timezone.now() - TIMEDELTAS[del_after]

    def update_unread_count(self):
        self.unread_count = self.entries.filter(read=False).count()
        Feed.objects.filter(pk=self.pk).update(
            unread_count=self.unread_count,
        )


class EntryManager(models.Manager):
    def unread(self):
        return self.filter(read=False).count()


class Entry(models.Model):
    """An entry is a cached feed item"""
    feed = models.ForeignKey(Feed, verbose_name=_('Feed'),
                             related_name='entries')
    title = models.CharField(_('Title'), max_length=255)
    subtitle = models.TextField(_('Abstract'))
    link = models.URLField(_('URL'), max_length=1023)
    # We also have a permalink for feed proxies (like FeedBurner). If the link
    # points to feedburner, the redirection (=real feed link) is put here
    permalink = models.URLField(_('Permalink'), max_length=1023, blank=True)
    date = models.DateTimeField(_('Date'), db_index=True)
    # The User FK is redundant but this may be better for performance and if
    # want to allow user input.
    user = models.ForeignKey(User, verbose_name=(_('User')),
                             related_name='entries')
    # Mark something as read or unread
    read = models.BooleanField(_('Read'), default=False, db_index=True)
    # Read later: store the URL
    read_later_url = models.URLField(_('Read later URL'), max_length=1023,
                                     blank=True)

    objects = EntryManager()

    ELEMENTS = (
        feedparser._HTMLSanitizer.acceptable_elements |
        feedparser._HTMLSanitizer.mathml_elements |
        feedparser._HTMLSanitizer.svg_elements
    )
    ATTRIBUTES = (
        feedparser._HTMLSanitizer.acceptable_attributes |
        feedparser._HTMLSanitizer.mathml_attributes |
        feedparser._HTMLSanitizer.svg_attributes
    )
    CSS_PROPERTIES = feedparser._HTMLSanitizer.acceptable_css_properties

    def __unicode__(self):
        return u'%s' % self.title

    def sanitized_title(self):
        if self.title:
            return bleach.clean(self.title, tags=[], strip=True)
        return _('(No title)')

    def sanitized_content(self):
        return bleach.clean(
            self.subtitle,
            tags=self.ELEMENTS,
            attributes=self.ATTRIBUTES,
            styles=self.CSS_PROPERTIES,
            strip=True,
        )

    def sanitized_nomedia_content(self):
        return bleach.clean(
            self.subtitle,
            tags=self.ELEMENTS - set(['img', 'audio', 'video']),
            attributes=self.ATTRIBUTES,
            styles=self.CSS_PROPERTIES,
            strip=True,
        )

    class Meta:
        # Display most recent entries first
        ordering = ('-date', 'title')
        verbose_name_plural = 'entries'

    def get_absolute_url(self):
        return reverse('feeds:item', args=[self.id])

    def get_link(self):
        if self.permalink:
            return self.permalink
        return self.link

    def link_domain(self):
        return urlparse.urlparse(self.get_link()).netloc

    def read_later_domain(self):
        netloc = urlparse.urlparse(self.read_later_url).netloc
        return netloc.replace('www.', '')

    def read_later(self):
        """Adds this item to the user's read list"""
        user = self.user
        if not user.read_later:
            return
        getattr(self, 'add_to_%s' % self.user.read_later)()

    def add_to_readitlater(self):
        url = 'https://readitlaterlist.com/v2/add'
        data = json.loads(self.user.read_later_credentials)
        data.update({
            'apikey': settings.API_KEYS['readitlater'],
            'url': self.get_link(),
            'title': self.title,
        })
        # The readitlater API doesn't return anything back
        requests.post(url, data=data)

    def add_to_readability(self):
        url = 'https://www.readability.com/api/rest/v1/bookmarks'
        client = self.oauth_client('readability')
        params = {'url': self.get_link()}
        response, data = client.request(url, method='POST',
                                        body=urllib.urlencode(params))
        response, data = client.request(response['location'], method='GET')
        url = 'https://www.readability.com/articles/%s'
        self.read_later_url = url % json.loads(data)['article']['id']
        self.save()

    def add_to_instapaper(self):
        url = 'https://www.instapaper.com/api/1/bookmarks/add'
        client = self.oauth_client('instapaper')
        params = {'url': self.get_link()}
        response, data = client.request(url, method='POST',
                                        body=urllib.urlencode(params))
        url = 'https://www.instapaper.com/read/%s'
        url = url % json.loads(data)[0]['bookmark_id']
        self.read_later_url = url
        self.save()

    def oauth_client(self, service):
        service_settings = getattr(settings, service.upper())
        consumer = oauth.Consumer(service_settings['CONSUMER_KEY'],
                                  service_settings['CONSUMER_SECRET'])
        creds = json.loads(self.user.read_later_credentials)
        token = oauth.Token(key=creds['oauth_token'],
                            secret=creds['oauth_token_secret'])
        client = oauth.Client(consumer, token)
        client.set_signature_method(oauth.SignatureMethod_HMAC_SHA1())
        return client


def pubsubhubbub_update(notification, **kwargs):
    parsed = notification
    url = None
    for link in parsed.feed.links:
        if link['rel'] == 'self':
            url = link['href']
    if url is None:
        return
    feeds = Feed.objects.filter(url=url)
    updater = FeedUpdater(parsed, feeds)
    updater.update()
updated.connect(pubsubhubbub_update)


class FaviconManager(models.Manager):
    def update_favicon(self, link, force_update=False):
        if not link:
            return
        parsed = list(urlparse.urlparse(link))
        if not parsed[0].startswith('http'):
            return
        favicon, created = self.get_or_create(url=link)
        urls = UniqueFeed.objects.filter(link=link).values_list('url',
                                                                flat=True)
        feeds = Feed.objects.filter(url__in=urls, favicon='')
        if not created and not force_update:
            # Still, add to existing
            favicon_url = self.filter(url=link).values_list('favicon',
                                                            flat=True)[0]
            if not favicon_url:
                return favicon

            if not feeds.exists():
                return

            feeds.update(favicon=favicon_url)
            return favicon

        ua = {'User-Agent': FAVICON_FETCHER}

        try:
            page = requests.get(link, headers=ua, timeout=10).content
        except requests.RequestException:
            return favicon
        if not page:
            return favicon

        icon_path = lxml.html.fromstring(page.lower()).xpath(
            '//link[@rel="icon" or @rel="shortcut icon"]/@href'
        )

        if not icon_path:
            parsed[2] = '/favicon.ico'  # 'path' element
            icon_path = [urlparse.urlunparse(parsed)]
        if not icon_path[0].startswith('http'):
            parsed[2] = icon_path[0]
            parsed[3] = parsed[4] = parsed[5] = ''
            icon_path = [urlparse.urlunparse(parsed)]
        try:
            response = requests.get(icon_path[0], headers=ua, timeout=10)
        except requests.RequestException:
            return favicon
        if response.status_code != 200:
            return favicon

        icon_file = ContentFile(response.content)
        m = magic.Magic()
        icon_type = m.from_buffer(response.content)
        if 'PNG' in icon_type:
            ext = 'png'
        elif 'MS Windows icon' in icon_type:
            ext = 'ico'
        elif 'GIF' in icon_type:
            ext = 'gif'
        elif 'JPEG' in icon_type:
            ext = 'jpg'
        elif 'PC bitmap' in icon_type:
            ext = 'bmp'
        elif icon_type == 'data':
            ext = 'ico'
        elif ('HTML' in icon_type or
              icon_type == 'empty' or
              'Photoshop' in icon_type or
              'ASCII' in icon_type):
            logger.debug("Ignored content type for %s: %s" % (link, icon_type))
            return favicon
        else:
            logger.info("Unknown content type for %s: %s" % (link, icon_type))
            favicon.delete()
            return

        filename = '%s.%s' % (urlparse.urlparse(favicon.url).netloc, ext)
        favicon.favicon.save(filename, icon_file)

        for feed in feeds:
            feed.favicon.save(filename, icon_file)
        return favicon


class Favicon(models.Model):
    url = models.URLField(_('Domain URL'), db_index=True)
    favicon = models.FileField(upload_to='favicons', blank=True,
                               storage=OverwritingStorage())

    objects = FaviconManager()

    def __unicode__(self):
        return u'Favicon for %s' % self.url

    def favicon_img(self):
        if not self.favicon:
            return '(None)'
        return '<img src="%s">' % self.favicon.url
    favicon_img.allow_tags = True
