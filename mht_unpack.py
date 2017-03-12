#!/usr/bin/env python3
"""
An MHTML message is a web archive format used by Internet Explorer and others. This encapsulates a portion of a website into a single file.

This script tries fairly hard to unpack an MHTML message into something most browsers can handle. If various modules are installed, it will minify javascript and css and transcode images.

It can either repack it into a single file using the data: URI standard, or into multiple files in the same directory.

It tries to pass test cases from http://people.dsv.su.se/~jpalme/mimetest/MHTML-test-messages.html

Usage:

  mht_unpack.py [file] [file] [file]
  mht_unpack_files [file] [file] [file]

Call it as mht_unpack_files if you want to unpack blobs into the directory. If 'files' shows up in the script name, it will unpack into the directory.
   Caveats: you'll get a bunch of blob=owefjwioefj=.whatever files.
       They do need to live in the same directory; I'd need to add some logic to handle references to HTML files.
       And MIME types aren't preserved, but this usually doesn't matter because file extensions handle the most common data types.

Call it as mht_unpack if you want to unpack into standalone HTML files using data: URIs. For a very simple HTML file and a modern browser, this works great.
   Caveats: it doesn't handle the fairly trivial case of a 5 page document with "next" and "previous" links.
       data: URIs can get *very* big, and they store everything redundantly.

The problem is that the data: URI has to represent everything that could be found in the link. The next and previous links are, obviously, circular, and while we break cycles, this gets pretty large.

License: it's not licensed for use, and is protected by copyright. I plan to license it under the same terms as Python, just need to find the proper boilerplate. Let me know if you actually want to use it. Since I haven't licensed you to use it, you're on your own if you do and it breaks something.

Copyright 2013, 2014 Ben Samuel
"""

import email as em
import sys
import base64 as b64
import urllib.parse as up
import bs4
try:
    import magic
    magic_obj = magic.Magic(flags=magic.MAGIC_NO_CHECK_TAR | magic.MAGIC_NO_CHECK_ELF | magic.MAGIC_MIME_TYPE)
except ImportError:
    magic_obj = None

try:
    from csscompressor import compress as _css_compress
    def css_compress(data):
        return _css_compress(bs4.UnicodeDammit(data).unicode_markup).encode("utf8")
except ImportError:
    css_compress = None

try:
    from rjsmin import jsmin as _js_compress
    def js_compress(data):
        return _js_compress(bs4.UnicodeDammit(data).unicode_markup).encode("utf8")
except ImportError:
    js_compress = None

try:
    from PIL import Image
    from io import BytesIO
except ImportError:
    Image = None

import hashlib as hl
import os.path as op
import mimetypes as mt

def suspect_mime_type(mime_type):
    '''
    These usually indicate detection failed.
    '''
    return not mime_type or mime_type in ('text/plain', 'application/octet-stream')

common_types = {
    'text/html': '.html',
    'text/plain': '.txt',
    'text/javascript': '.js',
    'application/javascript': '.js',
    'application/x-javascript': '.js',
    'text/css': '.css',
    'application/css': '.css',
    'application/octet-stream': '.data',
    'image/jpeg': '.jpg'
}

MAX_DIM = 1024 # images can't have a dimension larger than this

if Image:
    def jpeg_compress(buf):
        """
        Use PIL to resave an image using a low JPEG compression setting.
        """
        try:
            img = Image.open(BytesIO(buf))
        except OSError:
            return buf
        width, height = img.size
        new_width, new_height = width, height
        def constrain(longer, short):
            if longer > MAX_DIM:
                return MAX_DIM, int(short * MAX_DIM / longer)
            else:
                return longer, short
        if width > height:
            new_width, new_height = constrain(width, height)
        else:
            new_height, new_width = constrain(height, width)
        if (width, height) != (new_width, new_height):
            img = img.resize((new_width, new_height), Image.ANTIALIAS)
        newbuf = BytesIO()
        if img.mode == 'P':
            img.save(newbuf, "PNG", optimize=True)
            return (newbuf.getvalue(), 'image/png')
        else:
            img.save(newbuf, "JPEG", quality=30, optimize=True)
            return (newbuf.getvalue(), 'image/jpeg')
else:
    jpeg_compress = None

png_compress = jpeg_compress
gif_compress = jpeg_compress

minify = {
    'text/javascript': js_compress,
    'application/javascript': js_compress,
    'application/x-javascript': js_compress,
    'text/css': css_compress,
    'application/css': css_compress,
    'image/jpeg': jpeg_compress,
    'image/png': png_compress,
    'image/gif': gif_compress
}

def compress_data(data, mime_type):
    """
    Given a buffer of data and a mime type, returns a smaller buffer
    or the same one.
    """
    if not mime_type:
        return data, ""
    compressor = minify.get(mime_type.split(';')[0], None)
    if not compressor:
        return data, mime_type
    try:
        output = compressor(data)
    except:
        print("Error while compressing type ", mime_type, file=sys.stderr)
        raise
    if isinstance(output, tuple):
        new_data, new_mime = output
    else:
        new_data = output
        new_mime = mime_type
    if len(new_data) < len(data):
        return new_data, new_mime
    else:
        return data, mime_type

def find_extension(mime_type):
    """
    Determine an extension for a given mime type.
    """
    mime_type = mime_type.lower()
    try:
        ext = common_types[mime_type]
    except KeyError:
        # This almost never works, but oh well.
        common_types[mime_type] = ext = mt.guess_extension(mime_type) or ""
    return ext

class PartHelper:
    """
    Does some initial investigation of a MIME part.
    """
    def __init__(self, part, recommended_mime_type):
        self.part = part
        mime = part.get_content_type()
        payload = part.get_payload(decode=True)
        if suspect_mime_type(mime) and magic_obj:
            mime = magic_obj.id_buffer(payload)
        if suspect_mime_type(mime) and recommended_mime_type:
            mime = recommended_mime_type
        if not mime:
            mime = ""
        self.content_type = mime
        self.payload = payload
        self.extension = find_extension(mime)
        digest = hl.sha256(payload).digest()
        self.digest = b64.urlsafe_b64encode(digest).decode("ascii")

class InlineData:
    """
    A mixin to represent objects using inline data URIs.
    """
    def render_data(self, helper, seen):
        """
        Given a part, and a set of seen parts, render the data as a data: URI.
        :param part: the message part.
        :param seen: a set of seen parts.
        :return: a URI representing the data.
        """
        if helper.digest in seen:
            return None
        binary, content_type = self.render(helper, seen | { helper.digest })
        binary, content_type = compress_data(binary, content_type)
        return "data:{0};base64,{1}".format(
            content_type, b64.encodebytes(binary).decode()
            .replace("\n", ""))


class DataDirectory:
    """
    A mixin to represent objects using a folder of data files.
    """
    def render_data(self, helper, seen):
        """
        Given a part, and a set of seen parts, render the data as a relative URI.
        :param part: the message part.
        :param seen: a set of seen parts
        :return: a URI representing the data.
        """
        path = "blob={0}{1}".format(helper.digest, helper.extension)
        if not op.exists(path):
            with open(path, "ab") as fh:
                pass # touch the file to take responsibility for it.
            binary, content_type = self.render(helper, seen)
            binary, content_type = compress_data(binary, content_type)
            with open(path, "wb") as fh:
                fh.write(binary)
        return path


class Mapped:
    def __init__(self, mess):
        """
        Walks a multipart message and builds indexes into the parts using the content-Id and content-location headers.

        Also respects Content-Base, but apparently that's been dropped from the standard.
        :param mess: A message part generated by the standard email package.
        """
        self.by_loc = {}
        self.by_id = {}

        self.starts = set()
        for part in mess.walk():
            start = part.get_param('start', None)
            if start is not None:
                self.starts.add(start)
            base = part.get('Content-Base', "")
            loc = part.get('Content-Location', None)
            if loc is not None:
                self.by_loc[up.urljoin(base, loc)] = part
            cid = part.get('Content-ID', None)
            if cid is not None:
                self.by_id[cid] = self.by_id[cid.strip("<>")] = part

    def render(self, helper, seen=frozenset()):
        """
        Renders a message part.
        :param helper: a helper that holds a part of a multipart mime message, or a message part
        :param seen: a set used for cycle detection
        :return: a 2-tup of (binary, mimetype), where mimetype is e.g. "text/html"
        """
        if not isinstance(helper, PartHelper):
            helper = PartHelper(helper)
        data = helper.payload
        content_type = helper.content_type
        part = helper.part
        if content_type == "text/html":
            doc = bs4.BeautifulSoup(data)
            loc = part.get('Content-Location', "").strip()
            base = [up.urljoin(loc, base)
                    for base
                    in doc('base', limit=1) + [part.get('Content-Base', "")]][0]
            for tag in doc.descendants:
                if not isinstance(tag, bs4.Tag):
                    continue
                for attr in self.refs.get(tag.name, ()):
                    href = tag.get(attr, "").strip()
                    if not href:
                        continue
                    mime_type = tag.get('type', "").strip().lower()
                    href_split = up.urlsplit(href)
                    if href_split.scheme == 'cid':
                        mref = self.by_id.get(href_split.path, None)
                    else:
                        mref = self.by_loc.get(up.urljoin(base, href), None)
                    if mref:
                        href = self.render_data(PartHelper(mref, mime_type), seen)
                        if href is not None:
                            tag[attr] = href
            return doc.encode('utf-8'), 'text/html;charset=utf8'
        if isinstance(data, str):
            return data.encode('utf-8'), "{0};charset=utf8".format(content_type)
        return data, content_type

    refs = {
        'a': ['href'],
        'applet': ['codebase'],
        'area': ['href'],
        'audio': ['src'],
        'blockquote': ['cite'],
        'body': ['background'],
        'button': ['formaction'],
        'command': ['icon'],
        'del': ['cite'],
        'embed': ['src'],
        'form': ['action'],
        'frame': ['longdesc', 'src'],
        'head': ['profile'],
        'html': ['manifest'],
        'iframe': ['longdesc', 'src'],
        'img': ['longdesc', 'src', 'usemap'],
        'input': ['formaction', 'src', 'usemap'],
        'ins': ['cite'],
        'link': ['href'],
        'object': ['classid', 'codebase', 'data', 'usemap'],
        'q': ['cite'],
        'script': ['src'],
        'source': ['src'],
        'track': ['src'],
        'video': ['poster', 'src']
    }

class MappedInline(Mapped, InlineData):
    pass

class MappedRelative(Mapped, DataDirectory):
    pass

def convert_to_html(file_path,out_path):
    '''
    convert a mhtml to a html
    :param file_path:   [string]
    :param out_path:    [string]
    :return:
    '''

    con = MappedInline
    path = file_path

    with open(path, "rb") as fp:
        mess = em.message_from_binary_file(fp)
    mapper = con(mess)
    root = None
    for start in mapper.starts:
        root = mapper.by_id.get(start, None)
        if root is not None:
            break
    if root is None:
        for part in mess.walk():
            if not part.is_multipart():
                root = part
                break
    if root is None:
        print(path, ": Can't find root node")
        return False
    binary, mime = mapper.render(PartHelper(root, 'text/html'))
    new_path = op.splitext(path)[0] + ".conv.html"

    if out_path:
        new_path = out_path

    with open(new_path, "wb") as fp:
        fp.write(binary)

    return new_path




if __name__ == '__main__':
    if "file" in sys.argv[0]:
        con = MappedRelative
    else:
        con = MappedInline
    for path in sys.argv[1:]:
        print("Handling ", path, file=sys.stderr)
        with open(path, "rb") as fp:
            mess = em.message_from_binary_file(fp)
        mapper = con(mess)
        root = None
        for start in mapper.starts:
            root = mapper.by_id.get(start, None)
            if root is not None:
                break
        if root is None:
            for part in mess.walk():
                if not part.is_multipart():
                    root = part
                    break
        if root is None:
            print(path, ": Can't find root node", file=sys.stderr)
            continue
        binary, mime = mapper.render(PartHelper(root, 'text/html'))
        new_path = op.splitext(path)[0] + ".conv.html"
        with open(new_path, "wb") as fp:
            fp.write(binary)
