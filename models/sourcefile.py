import hashlib
import cStringIO
from os import path
from datetime import datetime
from urlparse import urlparse
import re

from tornado.escape import url_escape, json_decode, json_encode
from tornado.options import options
from PIL import Image
from lib.s3 import S3Bucket
from boto.s3.key import Key

from lib.flyingcow import Model, Property
from lib.flyingcow.cache import ModelQueryCache


class Sourcefile(ModelQueryCache, Model):
    width = Property()
    height = Property()
    data = Property()
    type = Property()
    file_key = Property()
    thumb_key = Property()
    small_key = Property()
    nsfw = Property(default=0)
    created_at = Property()
    updated_at = Property()

    def save(self, *args, **kwargs):
        """
        Sets the dates before saving.
        """
        if options.readonly:
            self.add_error('_', 'Site is read-only.')
            return False

        self._set_dates()
        super(Sourcefile, self).save(*args, **kwargs)

    def _set_dates(self):
        """
        Sets the created_at and updated_at fields. This should be something
        a subclass of Property that takes care of this during the save cycle.
        """
        if self.id is None or self.created_at is None:
            self.created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        self.updated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    def width_constrained_dimensions(self, width_constraint):
        """
        This is used to figure out the size at which something will render on
        a page, which is constrained with a width of 550px. For photos, the
        image size will be scaled down if it's too big, and otherwise, it will
        be presented as is. Videos also get scaled down if too big, but they also
        get enlarged to fit the width if smaller than constraint.
        """
        if self.width <= width_constraint and self.type == 'image':
            return (self.width, self.height)
        rescale_ratio = float(width_constraint) / self.width
        return (int(self.width * rescale_ratio), int(self.height * rescale_ratio))

    def nsfw_bool(self):
        """
        NSFW flag cast to a boolean.
        """
        if not self.nsfw or self.nsfw == 0:
            return False
        return True

    @staticmethod
    def get_by_file_key(file_key):
        """
        Returns a Sourcefile by its file_key.
        """
        return Sourcefile.get("file_key = %s", file_key)

    @staticmethod
    def get_sha1_url_key(url=None):
        if not url:
            return None
        h = hashlib.sha1()
        h.update(url)
        return h.hexdigest()

    @staticmethod
    def get_sha1_file_key(file_path=None, file_data=None):
        if not file_data:
            try:
                fh = open(file_path, 'r')
                file_data = fh.read()
                fh.close()
            except Exception as e:
                return None

        h = hashlib.sha1()
        h.update(file_data)
        return h.hexdigest()

    @staticmethod
    def get_from_file(file_path, sha1_value, type='image', skip_s3=None):
        existing_source_file = Sourcefile.get("file_key = %s", sha1_value)
        thumb_cstr =  cStringIO.StringIO()
        small_cstr =  cStringIO.StringIO()
        if existing_source_file:
            return existing_source_file
        try:
            img = Image.open(file_path)
            original_width = img.size[0]
            original_height= img.size[1]
            image_format = img.format
        except Exception as e:
            return None

        if img.mode != "RGB":
            img = img.convert("RGB")

        #generate smaller versions
        thumb = img.copy()
        small = img.copy()

        thumb.thumbnail((100,100), Image.ANTIALIAS)
        small.thumbnail((240,184), Image.ANTIALIAS)

        thumb.save(thumb_cstr, format="JPEG")
        small.save(small_cstr, format="JPEG")

        bucket = None
        if not skip_s3:
            bucket = S3Bucket()

        #save original file
        if type != 'link':
            if not skip_s3:
                k = Key(bucket)
                k.key = "originals/%s" % (sha1_value)
                k.set_contents_from_filename(file_path)

        #save thumbnail
        thumbnail_file_key = Sourcefile.get_sha1_file_key(file_data=thumb_cstr.getvalue())
        if not skip_s3:
            k = Key(bucket)
            k.key = "thumbnails/%s" % thumbnail_file_key
            k.set_contents_from_string(thumb_cstr.getvalue())

        #save small
        small_file_key = Sourcefile.get_sha1_file_key(file_data=small_cstr.getvalue())
        if not skip_s3:
            k = Key(bucket)
            k.key = "smalls/%s" % small_file_key
            k.set_contents_from_string(small_cstr.getvalue())

        #save source file
        sf = Sourcefile(width=original_width, height=original_height, file_key=sha1_value, thumb_key=thumbnail_file_key, small_key=small_file_key, type=type)
        sf.save()
        return sf

    @staticmethod
    def make_oembed_url(url):
        url_parsed = None
        try:
            url_parsed = urlparse(url)
        except:
            return None

        if url_parsed.hostname.lower() not in ['youtube.com', 'www.youtube.com', 'vimeo.com', 'www.vimeo.com', 'youtu.be', 'flickr.com', 'www.flickr.com', 'vine.co', 'www.vine.co']:
            return None

        oembed_url = None
        if url_parsed.hostname.lower() in ['youtube.com', 'www.youtube.com', 'youtu.be']:
            to_url = 'http://%s%s?%s' % (url_parsed.hostname, url_parsed.path, url_parsed.query)
            oembed_url = 'http://www.youtube.com/oembed?url=%s&maxwidth=550&format=json' % (url_escape(to_url))
        elif url_parsed.hostname.lower() in ['vimeo.com', 'www.vimeo.com']:
            to_url = 'http://%s%s' % (url_parsed.hostname, url_parsed.path)
            oembed_url = 'http://vimeo.com/api/oembed.json?url=%s&maxwidth=550' % (url_escape(to_url))
        elif url_parsed.hostname.lower() in ['flickr.com', 'www.flickr.com']:
            to_url = 'http://%s%s' % (url_parsed.hostname, url_parsed.path)
            oembed_url = 'http://www.flickr.com/services/oembed/?url=%s&maxwidth=550&format=json' % (url_escape(to_url))
        elif url_parsed.hostname.lower() in ['vine.co', 'www.vine.co']:
            clean_path = re.search('^(/v/[a-zA-Z0-9]+)', url_parsed.path)
            if clean_path and clean_path.group(1):
                to_url = 'https://vine.co%s' % (clean_path.group(1))
                iframe = """<iframe class="vine-embed" src="%s/embed/simple" """ \
                    """width="480" height="480" frameborder="0">""" \
                    """</iframe><script async src="//platform.vine.co/static/scripts/embed.js" """ \
                    """charset="utf-8"></script>""" % to_url
                oembed_url = 'data:text/json;charset=utf-8,{"provider_name":"Vine","title":"","html":"%s"}' % iframe.replace('"', '\\"')
        return oembed_url

    @staticmethod
    def create_from_json_oembed(link=None, oembed_doc=None, thumbnail_file_path=None):
        """
        Ideally this is a link right now. Specificallly a video link.

        JSON object, thumbnail_path, and the actual url comes in, a sha1 should be created from the url and the
            file_key takes that sha1 value. Then call get_from_file with the type=link
            value set along with the thumbnail path in place.

            The resulting sourcefile should then have the data field set with the oembed doc.

            A source file should be created and returned.
        """
        sha1_key = Sourcefile.get_sha1_file_key(file_path=None, file_data=link)
        sf = Sourcefile.get_from_file(thumbnail_file_path, sha1_key, type='link')
        if sf:
            sf.data = json_encode(oembed_doc)
            sf.save()
        return sf


