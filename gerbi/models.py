# -*- coding: utf-8 -*-
"""Django page CMS ``models``."""

from gerbi.utils import get_placeholders, normalize_url
from gerbi.managers import PageManager, ContentManager
from gerbi.managers import PageAliasManager
from gerbi import settings

from datetime import datetime
from django.db import models
from django.contrib.auth.models import User
from django.utils.translation import ugettext_lazy as _
from django.utils.safestring import mark_safe
from django.core.cache import cache
from django.core.urlresolvers import reverse
from django.contrib.sites.models import Site

from mptt.models import MPTTModel
if settings.GERBI_TAGGING:
    from taggit.managers import TaggableManager

GERBI_CONTENT_DICT_KEY = ContentManager.GERBI_CONTENT_DICT_KEY


class Page(MPTTModel):
    """
    This model contain the status, dates, author, template.
    The real content of the page can be found in the
    :class:`Content <gerbi.models.Content>` model.

    .. attribute:: creation_date
       When the page has been created.

    .. attribute:: publication_date
       When the page should be visible.

    .. attribute:: publication_end_date
       When the publication of this page end.

    .. attribute:: last_modification_date
       Last time this page has been modified.

    .. attribute:: status
       The current status of the page. Could be DRAFT, PUBLISHED,
       EXPIRED or HIDDEN. You should the property :attr:`calculated_status` if
       you want that the dates are taken in account.

    .. attribute:: template
       A string containing the name of the template file for this page.
    """

    # some class constants to refer to, e.g. Page.DRAFT
    DRAFT = 0
    PUBLISHED = 1
    EXPIRED = 2
    HIDDEN = 3
    STATUSES = (
        (PUBLISHED, _('Published')),
        (HIDDEN, _('Hidden')),
        (DRAFT, _('Draft')),
    )

    GERBI_URL_KEY = "page_%d_url"
    GERBI_BROKEN_LINK_KEY = "page_broken_link_%s"

    author = models.ForeignKey(User, verbose_name=_('author'))

    parent = models.ForeignKey('self', null=True, blank=True,
            related_name='children', verbose_name=_('parent'))
    creation_date = models.DateTimeField(_('creation date'), editable=False,
            default=datetime.now)
    publication_date = models.DateTimeField(_('publication date'),
            null=True, blank=True, help_text=_('''When the page should go
            live. Status must be "Published" for page to go live.'''))
    publication_end_date = models.DateTimeField(_('publication end date'),
            null=True, blank=True, help_text=_('''When to expire the page.
            Leave empty to never expire.'''))

    last_modification_date = models.DateTimeField(_('last modification date'))

    status = models.IntegerField(_('status'), choices=STATUSES, default=DRAFT)
    template = models.CharField(_('template'), max_length=100, null=True,
            blank=True)

    delegate_to = models.CharField(_('delegate to'), max_length=100, null=True,
            blank=True)

    freeze_date = models.DateTimeField(_('freeze date'),
            null=True, blank=True, help_text=_("""Don't publish any content
            after this date."""))

    if settings.GERBI_USE_SITE_ID:
        sites = models.ManyToManyField(Site, default=[settings.SITE_ID],
                help_text=_('The site(s) the page is accessible at.'),
                verbose_name=_('sites'))

    redirect_to_url = models.CharField(max_length=200, null=True, blank=True)

    redirect_to = models.ForeignKey('self', null=True, blank=True,
            related_name='redirected_pages')

    # Managers
    objects = PageManager()

    if settings.GERBI_TAGGING:
        tags = TaggableManager()

    class Meta:
        """Make sure the default page ordering is correct."""
        ordering = ['tree_id', 'lft']
        # Gerbi module was once called pages
        get_latest_by = "publication_date"
        verbose_name = _('page')
        verbose_name_plural = _('pages')
        permissions = settings.GERBI_EXTRA_PERMISSIONS

    def __init__(self, *args, **kwargs):
        """Instanciate the page object."""
        # per instance cache
        self.cache = None
        self._languages = None
        self._complete_slug = None
        self._is_first_root = None
        super(Page, self).__init__(*args, **kwargs)

    def save(self, *args, **kwargs):
        """Override the default ``save`` method."""
        if not self.status:
            self.status = self.DRAFT
        # Published gerbi should always have a publication date
        if self.publication_date is None and self.status == self.PUBLISHED:
            self.publication_date = datetime.now()
        # Drafts should not, unless they have been set to the future
        if self.status == self.DRAFT:
            if settings.GERBI_SHOW_START_DATE:
                if (self.publication_date and
                        self.publication_date <= datetime.now()):
                    self.publication_date = None
            else:
                self.publication_date = None
        self.last_modification_date = datetime.now()
        # let's assume there is no more broken links after a save
        cache.delete(self.GERBI_BROKEN_LINK_KEY % self.id)
        super(Page, self).save(*args, **kwargs)
        self.invalidate()
        # fix sites many-to-many link when the're hidden from the form
        if settings.GERBI_HIDE_SITES and self.sites.count() == 0:
            self.sites.add(Site.objects.get(pk=settings.SITE_ID))

    def _get_calculated_status(self):
        """Get the calculated status of the page based on
        :attr:`Page.publication_date`,
        :attr:`Page.publication_end_date`,
        and :attr:`Page.status`."""
        if settings.GERBI_SHOW_START_DATE and self.publication_date:
            if self.publication_date > datetime.now():
                return self.DRAFT

        if settings.GERBI_SHOW_END_DATE and self.publication_end_date:
            if self.publication_end_date < datetime.now():
                return self.EXPIRED

        return self.status
    calculated_status = property(_get_calculated_status)

    def _visible(self):
        """Return True if the page is visible on the frontend."""
        return self.calculated_status in (self.PUBLISHED, self.HIDDEN)
    visible = property(_visible)

    def get_children_for_frontend(self):
        """Return a :class:`QuerySet` of published children page"""
        return Page.objects.filter_published(self.get_children())

    def get_date_ordered_children_for_frontend(self):
        """Return a :class:`QuerySet` of published children page ordered
        by publication date."""
        return self.get_children_for_frontend().order_by('-publication_date')

    def invalidate(self):
        """Invalidate cached data for this page."""
        cache.delete('GERBI_FIRST_ROOT_ID')
        key = "gerbi_page_%d" % (self.id)
        cache.delete(key)
        self._languages = None
        self._complete_slug = None
        self.cache = None
        cache.delete(self.GERBI_URL_KEY % (self.id))

    def get_languages(self):
        """
        Return a list of all used languages for this page.
        """
        if self._languages:
            return self._languages

        self._languages = []
        # this a very expensive operation if there is
        # many languages
        self.build_cache()
        for lang in settings.GERBI_LANGUAGES:
            lang = lang[0]
            if lang in self.cache and self.cache[lang]:
                self._languages.append(lang)

        return self._languages

    def is_first_root(self):
        """Return ``True`` if this page is the first root gerbi."""
        if self.parent:
            return False
        if self._is_first_root is not None:
            return self._is_first_root
        first_root_id = cache.get('GERBI_FIRST_ROOT_ID')
        if first_root_id is not None:
            self._is_first_root = first_root_id == self.id
            return self._is_first_root
        try:
            first_root_id = Page.objects.root().values('id')[0]['id']
        except IndexError:
            first_root_id = None
        if first_root_id is not None:
            cache.set('GERBI_FIRST_ROOT_ID', first_root_id)
        self._is_first_root = self.id == first_root_id
        return self._is_first_root

    def get_url_path(self, language=None):
        """Return the URL's path component. Add the language prefix if
        ``GERBI_USE_LANGUAGE_PREFIX`` setting is set to ``True``.

        :param language: the wanted url language.
        """
        if self.is_first_root():
            # this is used to allow users to change URL of the root
            # page. The language prefix is not usable here.
            try:
                return reverse('django-gerbi-root')
            except Exception:
                pass
        url = self.get_complete_slug(language)
        if not language:
            language = settings.GERBI_DEFAULT_LANGUAGE
        if settings.GERBI_USE_LANGUAGE_PREFIX:
            return reverse('django-gerbi-details-by-path',
                args=[language, url])
        else:
            return reverse('django-gerbi-details-by-path', args=[url])

    def get_absolute_url(self, language=None):
        """Alias for `get_url_path`.

        This method is only there for backward compatibility and will be
        removed in a near futur.

        :param language: the wanted url language.
        """
        return self.get_url_path(language=language)

    def get_complete_slug(self, language=None, hideroot=True):
        """Return the complete slug of this page by concatenating
        all parent's slugs.

        :param language: the wanted slug language."""
        if not language:
            language = settings.GERBI_DEFAULT_LANGUAGE

        if self._complete_slug and language in self._complete_slug:
            return self._complete_slug[language]

        self._complete_slug = cache.get(self.GERBI_URL_KEY % (self.id))
        if self._complete_slug is None:
            self._complete_slug = {}
        elif language in self._complete_slug:
            return self._complete_slug[language]

        if hideroot and settings.GERBI_HIDE_ROOT_SLUG and self.is_first_root():
            url = u''
        else:
            url = u'%s' % self.slug(language)
        for ancestor in self.get_ancestors(ascending=True):
            url = ancestor.slug(language) + u'/' + url

        self._complete_slug[language] = url
        cache.set(self.GERBI_URL_KEY % (self.id), self._complete_slug)
        return url

    def get_url(self, language=None):
        """Alias for `get_complete_slug`.

        This method is only there for backward compatibility and will be
        removed in a near futur.

        :param language: the wanted url language.
        """
        return self.get_complete_slug(language=language)

    def slug(self, language=None, fallback=True):
        """
        Return the slug of the page depending on the given language.

        :param language: wanted language, if not defined default is used.
        :param fallback: if ``True``, the slug will also be searched in other \
        languages.
        """
        return self.get_content(language, 'slug', language_fallback=fallback)

    def title(self, language=None, fallback=True):
        """
        Return the title of the page depending on the given language.

        :param language: wanted language, if not defined default is used.
        :param fallback: if ``True``, the slug will also be searched in \
        other languages.
        """
        return self.get_content(language, 'title', language_fallback=fallback)

    def get_content(self, language, ctype, language_fallback=False):
        """Shortcut method for retrieving a piece of page content

        :param language: wanted language, if not defined default is used.
        :param ctype: the type of content.
        :param fallback: if ``True``, the content will also be searched in \
        other languages.
        """
        return Content.objects.get_content(self, language, ctype,
            language_fallback)

    def expose_content(self):
        """Return all the current content of this page into a `string`.

        This is used by the haystack framework to build the search index."""
        placeholders = get_placeholders(self.get_template())
        exposed_content = []
        for lang in self.get_languages():
            for ctype in [p.name for p in placeholders]:
                content = self.get_content(lang, ctype, False)
                if content:
                    exposed_content.append(content)
        return u"\r\n".join(exposed_content)

    def content_by_language(self, language):
        """
        Return a list of latest published
        :class:`Content <gerbi.models.Content>`
        for a particluar language.

        :param language: wanted language,
        """
        placeholders = get_placeholders(self.get_template())
        content_list = []
        for ctype in [p.name for p in placeholders]:
            try:
                content = Content.objects.get_content_object(self,
                    language, ctype)
                content_list.append(content)
            except Content.DoesNotExist:
                pass
        return content_list

    def get_template(self):
        """
        Get the :attr:`template <Page.template>` of this page if
        defined or the closer parent's one if defined
        or :attr:`gerbi.settings.GERBI_DEFAULT_TEMPLATE` otherwise.
        """
        if self.template:
            return self.template

        template = None
        for p in self.get_ancestors(ascending=True):
            if p.template:
                template = p.template
                break

        if not template:
            template = settings.GERBI_DEFAULT_TEMPLATE

        return template

    def get_template_name(self):
        """
        Get the template name of this page if defined or if a closer
        parent has a defined template or
        :data:`gerbi.settings.GERBI_DEFAULT_TEMPLATE` otherwise.
        """
        template = self.get_template()
        page_templates = settings.get_page_templates()
        for t in page_templates:
            if t[0] == template:
                return t[1]
        return template

    def has_broken_link(self):
        """
        Return ``True`` if the page have broken links to other pages
        into the content.
        """
        return cache.get(self.GERBI_BROKEN_LINK_KEY % self.id)

    def valid_targets(self):
        """Return a :class:`QuerySet` of valid targets for moving a page
        into the tree.

        :param perms: the level of permission of the concerned user.
        """
        exclude_list = [self.id]
        for p in self.get_descendants():
            exclude_list.append(p.id)
        return Page.objects.exclude(id__in=exclude_list)

    def build_cache(self, language=None):

        if not self.id:
            raise ValueError("Page have no id")
        key = "gerbi_page_%d" % (self.id)
        self.cache = self.cache or cache.get(key)
        if self.cache is None:
            self.cache = {}

        if language:
            languages = [language]
        else:
            languages = [lang[0] for lang in settings.GERBI_LANGUAGES]

        needed_languages = [lang for lang in languages
            if lang not in self.cache.keys()]
        # fill the cache for each needed language, that will create
        # P * L queries.
        # L == number of language, P == number of placeholder in the page.
        # Once generated the result is cached.
        for lang in needed_languages:
            self.cache[lang] = {}
            params = {
                'language': lang,
                'page': self
            }
            if self.freeze_date:
                params['creation_date__lte'] = self.freeze_date
            # get all the content types for one language
            for content_type in Content.objects.filter(**params).values(
                    'type').annotate(models.Max("creation_date")):
                ctype = content_type['type']

                try:
                    content = Content.objects.get_content_object(self, lang, ctype)
                    self.cache[lang][ctype] = {'body': content.body,
                        'creation_date': content.creation_date}
                except Content.DoesNotExist:
                    pass#self.cache[lang][p_name] = None

        if len(needed_languages):
            cache.set(key, self.cache)
        return self.cache

    def slug_with_level(self, language=None):
        """Display the slug of the page prepended with insecable
        spaces equal to simluate the level of page in the hierarchy."""
        level = ''
        if self.level:
            for n in range(0, self.level):
                level += '&nbsp;&nbsp;&nbsp;'
        return mark_safe(level + self.slug(language))

    def margin_level(self):
        """Used in the admin menu to create the left margin."""
        return self.level * 2

    def __unicode__(self):
        """Representation of the page, saved or not."""
        if self.id:
            # without ID a slug cannot be retrieved
            slug = self.slug()
            if slug:
                return slug
            return u"Page %d" % self.id
        return u"Page without id"

    def get_next_in_book( self ):
        """Returns the next page in the tree as if it was traversed
        like a book (see the tree as the book's ToC).

        This is exactly what a step-wise prefix DFS tree traversal would look like.

        Note that this implementation is biased as follows: we assume
        that each tree is independant, i.e. we make sure never to jump
        beyond a root node to another tree.
        """
        chd = self.get_children()
        if 0 < chd.count():
            return chd[0]
        elif self.get_next_sibling():
            return self.get_next_sibling()
        else:
            cnt = self.get_ancestors( ascending=True ).count()
            # Not interested by the root don't want to 'switch' tree.
            anc = self.get_ancestors( ascending=True )[0:cnt-1]

            for par in anc:
                nxt = par.get_next_sibling()
                if None != nxt:
                    return nxt
        return None

    def get_prev_in_book(self):
        """Returns the previous page in the tree as if it was
        traversed like a book.

        This is a step-wise postfix “reverse-DFS” traversal.

        Note that this implementation is biased as follows: we assume
        that each tree is independant, i.e. we make sure never to jump
        beyond a root node to another tree.
        """

        ## get_descendants() looks expansive: load the whole pages ?
        ## Better way to do that ? trying get_descendant_count() + []

        if not self.is_root_node():
            sib = self.get_previous_sibling()
            if None != sib:
                ## Somehow MPTT get_descendant_count() sometimes
                ## returns non zero value when descendant nodes have
                ## been added and then deleted. I guess this is an
                ## MPTT bug (not a PageCMS bug) that needs to be
                ## investigated.
                
                # cnt = sib.get_descendant_count()
                # if 0 == cnt:
                #     return sib
                # else: # cnt > 0
                #     return sib.get_descendants()[cnt-1]

                ## Inefficient fix.
                dsc = sib.get_descendants() ## Possibly very expansive
                cnt = len(dsc)
                if 0 == len(dsc):
                    return sib
                return dsc[cnt-1]
            else:
                # Should be as simple as self.parent but this breaks
                # ability to proxy the Page model.
                return self.__class__.objects.get(id=self.parent.id)
        return None

    """
    MPTT Methods override to enable proxying.
    """

    def get_ancestors(self, ascending=False, include_self=False):
        """
        Overloads get_children so it returns MESProxyPage (not Page)
        instances.
       
        Cannot use the same simple trick as in other methods: qset is
        sorted which trigers object instanciations.
        """
        qset = MPTTModel.get_ancestors( self, ascending=ascending,
                                        include_self=include_self )
        if qset:
            ## Somehow Pages in qset do get instanciated...
            qset._result_cache = None
            qset.model         = self.__class__           
        return qset


    def get_children ( self ):
        """
        Overloads get_children so it returns MESProxyPage (not Page)
        instances.
        """
        qset = MPTTModel.get_children( self )
        if qset:
            qset._result_cache = None
            qset.model         = self.__class__
        return qset


    def get_descendants(self, include_self=False):
        qset = MPTTModel.get_descendants( self, include_self=include_self )
        if qset:
            qset._result_cache = None
            qset.model         = self.__class__
        return qset


    def get_leafnodes(self, include_self=False):
        qset = MPTTModel.get_leafnodes( self, include_self=include_self )
        if qset:
            qset._result_cache = None
            qset.model         = self.__class__
        return qset


    def get_next_sibling(self, **filters):
        qset =  MPTTModel.get_next_sibling( self, **filters )
        if qset:
            qset._result_cache = None
            qset.model         = self.__class__
        return qset
   

    def get_previous_sibling(self, **filters):
        qset = MPTTModel.get_previous_sibling( self, **filters )
        if qset:
            qset._result_cache = None
            qset.model         = self.__class__
        return qset

    def get_root(self):
        qset = MPTTModel.get_root( self )
        if qset:
            qset._result_cache = None
            qset.model         = self.__class__
        return qset


    def get_siblings(self, include_self=False):
        qset = MPTTModel.get_siblings( self, include_self=include_self )
        if qset:
            qset._result_cache = None
            qset.model         = self.__class__
        return qset

    """
    Wrappers around attributes that points to related objects.
    
    Basically you never can traverse a relation directly through
    attribute: self.related, you need this kind of wrapper
    self.get_related(). Obviously, this has a cost...
    """

    def get_page_to_redirect_to( self ):
        """
        Helper to make redirection work with proxying.
        """
        if self.redirect_to is not None:
            return self.__class__.objects.get(pk=self.redirect_to.pk)
        return None


    def get_parent( self ):
        if self.parent is not None:
            return self.__class__.objects.get(pk=self.parent.pk)
        return None


class Content(models.Model):
    """A block of content, tied to a :class:`Page <gerbi.models.Page>`,
    for a particular language"""

    # languages could have five characters : Brazilian Portuguese is pt-br
    language = models.CharField(_('language'), max_length=5, blank=False)
    body = models.TextField(_('body'))
    type = models.CharField(_('type'), max_length=100, blank=False,
        db_index=True)
    page = models.ForeignKey(Page, verbose_name=_('page'))

    creation_date = models.DateTimeField(_('creation date'), editable=False,
        default=datetime.now)
    objects = ContentManager()

    def save(self, *args, **kwargs):
        self.page.invalidate()
        super(Content, self).save(*args, **kwargs)

    class Meta:
        get_latest_by = 'creation_date'
        verbose_name = _('content')
        verbose_name_plural = _('contents')

    def __unicode__(self):
        return u"%s :: %s" % (self.page.slug(), self.body[0:15])


class PageAlias(models.Model):
    """URL alias for a :class:`Page <gerbi.models.Page>`"""
    page = models.ForeignKey(Page, null=True, blank=True,
        verbose_name=_('page'))
    url = models.CharField(max_length=255, unique=True)
    objects = PageAliasManager()

    class Meta:
        verbose_name_plural = _('Aliases')

    def save(self, *args, **kwargs):
        # normalize url
        self.url = normalize_url(self.url)
        super(PageAlias, self).save(*args, **kwargs)

    def __unicode__(self):
        return u"%s :: %s" % (self.url, self.page.get_complete_slug())
