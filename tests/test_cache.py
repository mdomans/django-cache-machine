from django.conf import settings
from django.core.cache import cache

import jinja2
import mock
from nose.tools import eq_

from test_utils import ExtraAppTestCase
import caching.base as caching

from testapp.models import Addon, User


class CachingTestCase(ExtraAppTestCase):
    fixtures = ['testapp/test_cache.json']
    extra_apps = ['tests.testapp']

    def setUp(self):
        cache.clear()
        self.old_timeout = getattr(settings, 'CACHE_COUNT_TIMEOUT', None)

    def tearDown(self):
        settings.CACHE_COUNT_TIMEOUT = self.old_timeout

    def test_flush_key(self):
        """flush_key should work for objects or strings."""
        a = Addon.objects.get(id=1)
        eq_(caching.flush_key(a.cache_key), caching.flush_key(a))

    def test_cache_key(self):
        a = Addon.objects.get(id=1)
        eq_(a.cache_key, 'o:testapp.addon:1')

        keys = set((a.cache_key, a.author1.cache_key, a.author2.cache_key))
        eq_(set(a._cache_keys()), keys)

    def test_cache(self):
        """Basic cache test: second get comes from cache."""
        assert Addon.objects.get(id=1).from_cache is False
        assert Addon.objects.get(id=1).from_cache is True

    def test_invalidation(self):
        assert Addon.objects.get(id=1).from_cache is False
        a = [x for x in Addon.objects.all() if x.id == 1][0]
        assert a.from_cache is False

        assert Addon.objects.get(id=1).from_cache is True
        a = [x for x in Addon.objects.all() if x.id == 1][0]
        assert a.from_cache is True

        a.save()
        assert Addon.objects.get(id=1).from_cache is False
        a = [x for x in Addon.objects.all() if x.id == 1][0]
        assert a.from_cache is False

    def test_fk_invalidation(self):
        """When an object is invalidated, its foreign keys get invalidated."""
        a = Addon.objects.get(id=1)
        assert User.objects.get(name='clouseroo').from_cache is False
        a.save()

        assert User.objects.get(name='clouseroo').from_cache is False

    def test_fk_parent_invalidation(self):
        """When a foreign key changes, any parent objects get invalidated."""
        assert Addon.objects.get(id=1).from_cache is False
        a = Addon.objects.get(id=1)
        assert a.from_cache is True

        u = User.objects.get(id=a.author1.id)
        assert u.from_cache is True
        u.name = 'fffuuu'
        u.save()

        assert User.objects.get(id=a.author1.id).from_cache is False
        a = Addon.objects.get(id=1)
        assert a.from_cache is False
        eq_(a.author1.name, 'fffuuu')

    def test_raw_cache(self):
        sql = 'SELECT * FROM %s WHERE id = 1' % Addon._meta.db_table
        raw = list(Addon.objects.raw(sql))
        eq_(len(raw), 1)
        raw_addon = raw[0]
        a = Addon.objects.get(id=1)
        for field in Addon._meta.fields:
            eq_(getattr(a, field.name), getattr(raw_addon, field.name))
        assert raw_addon.from_cache is False

        cached = list(Addon.objects.raw(sql))
        eq_(len(cached), 1)
        cached_addon = cached[0]
        a = Addon.objects.get(id=1)
        for field in Addon._meta.fields:
            eq_(getattr(a, field.name), getattr(cached_addon, field.name))
        assert cached_addon.from_cache is True

    def test_raw_cache_params(self):
        """Make sure the query params are included in the cache key."""
        sql = 'SELECT * from %s WHERE id = %%s' % Addon._meta.db_table
        raw = list(Addon.objects.raw(sql, [1]))[0]
        eq_(raw.id, 1)

        raw2 = list(Addon.objects.raw(sql, [2]))[0]
        eq_(raw2.id, 2)

    @mock.patch('caching.base.cache')
    def test_count_cache(self, cache_mock):
        settings.CACHE_COUNT_TIMEOUT = 60
        cache_mock.scheme = 'memcached'
        cache_mock.get.return_value = None

        q = Addon.objects.all()
        count = q.count()

        args, kwargs = cache_mock.set.call_args
        key, value, timeout = args
        eq_(value, 2)
        eq_(timeout, 60)

    @mock.patch('caching.base.cached')
    def test_count_none_timeout(self, cached_mock):
        settings.CACHE_COUNT_TIMEOUT = None
        Addon.objects.count()
        eq_(cached_mock.call_count, 0)

    def test_queryset_flush_list(self):
        """Check that we're making a flush list for the queryset."""
        q = Addon.objects.all()
        assert cache.get(q.flush_key()) is None
        objects = list(q)  # Evaluate the queryset so it gets cached.

        query_key = cache.get(q.flush_key())
        assert query_key is not None
        eq_(list(cache.get(query_key.pop())), objects)

    def test_jinja_cache_tag_queryset(self):
        env = jinja2.Environment(extensions=['caching.ext.cache'])
        def check(q, expected):
            list(q) # Get the queryset in cache.
            t = env.from_string(
                "{% cache q %}{% for x in q %}{{ x.id }}:{{ x.val }};"
                "{% endfor %}{% endcache %}")
            s = t.render(q=q)

            eq_(s, expected)

            # Check the flush keys, find the key for the template.
            flush = cache.get(q.flush_key())
            eq_(len(flush), 2)

            # Check the cached fragment.  The key happens to be the first one,
            # according to however set arranges them.
            key = list(flush)[0]
            cached = cache.get(key)
            eq_(s, cached)

        check(Addon.objects.all(), '1:42;2:42;')
        check(Addon.objects.all(), '1:42;2:42;')

        # Make changes, make sure we dropped the cached fragment.
        a = Addon.objects.get(id=1)
        a.val = 17
        a.save()

        q = Addon.objects.all()
        flush = cache.get(q.flush_key())
        assert cache.get(q.flush_key()) is None

        check(Addon.objects.all(), '1:17;2:42;')
        check(Addon.objects.all(), '1:17;2:42;')

    def test_jinja_cache_tag_object(self):
        env = jinja2.Environment(extensions=['caching.ext.cache'])
        addon = Addon.objects.get(id=1)

        def check(obj, expected):
            t = env.from_string(
                '{% cache obj, 30 %}{{ obj.id }}:{{ obj.val }}{% endcache %}')
            eq_(t.render(obj=obj), expected)

        check(addon, '1:42')
        addon.val = 17
        addon.save()
        check(addon, '1:17')

    def test_jinja_multiple_tags(self):
        env = jinja2.Environment(extensions=['caching.ext.cache'])
        addon = Addon.objects.get(id=1)
        template = ("{% cache obj %}{{ obj.id }}{% endcache %}\n"
                    "{% cache obj %}{{ obj.val }}{% endcache %}")

        def check(obj, expected):
            t = env.from_string(template)
            eq_(t.render(obj=obj), expected)

        check(addon, '1\n42')
        addon.val = 17
        addon.save()
        check(addon, '1\n17')
