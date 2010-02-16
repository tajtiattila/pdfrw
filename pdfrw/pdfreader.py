# A part of pdfrw (pdfrw.googlecode.com)
# Copyright (C) 2006-2009 Patrick Maupin, Austin, Texas
# MIT license -- See LICENSE.txt for details

'''
The PdfReader class reads an entire PDF file into memory and
parses the top-level container objects.  (It does not parse
into streams.)  The object subclasses PdfDict, and the
document pages are stored in a list in the pages attribute
of the object.
'''

try:
    set
except NameError:
    from sets import Set as set

from pdftokens import PdfTokens
from pdfobjects import PdfDict, PdfArray, PdfName
from pdfcompress import uncompress

class PdfReader(PdfDict):

    class DeferredObject(object):
        pass

    def readindirect(self, objnum, gennum, parent, index,
                     Deferred=DeferredObject, int=int, isinstance=isinstance):
        ''' Read an indirect object.  If it has already
            been read, return it from the cache.
        '''
        key = int(objnum), int(gennum)
        result = self.indirect_objects.get(key)
        if result is None:
            result = Deferred()
            result.key = key
            result.usedby = []
            self.indirect_objects[key] = result
            self.deferred.add(result)
        if isinstance(result, Deferred):
            result.usedby.append((parent, index))
        return result

    def readarray(self, source, PdfArray=PdfArray, len=len):
        specialget = self.special.get
        result = PdfArray()
        pop = result.pop
        append = result.append

        for value in source:
            if value in ']R':
                if value == ']':
                    break
                generation = pop()
                value = self.readindirect(pop(), generation, result, len(result))
            else:
                func = specialget(value)
                if func is not None:
                    value = func(source)
            append(value)
        return result

    def readdict(self, source, PdfDict=PdfDict):
        specialget = self.special.get
        result = PdfDict()
        next = source.next

        tok = next()
        while tok != '>>':
            assert tok.startswith('/'), (tok, source.multiple(10))
            key = tok
            value = next()
            func = specialget(value)
            if func is not None:
                value = func(source)
                tok = next()
            else:
                tok = next()
                if value.isdigit() and tok.isdigit():
                    assert next() == 'R'
                    value = self.readindirect(value, tok, result, key)
                    tok = next()
            result[key] = value
        return result

    def findstream(obj, source, PdfDict=PdfDict, isinstance=isinstance, len=len):
        ''' Figure out if there is a content stream
            following an object, and return the start
            pointer to the content stream if so.

            (We can't read it yet, because we might not
            know how long it is, because Length might
            be an indirect object.)
        '''
        tok = source.next()
        if tok == 'endobj':
            return  # No stream

        assert isinstance(obj, PdfDict)
        assert tok == 'stream', tok
        fdata = source.fdata
        floc = fdata.rfind(tok, 0, source.floc) + len(tok)
        ch = fdata[floc]
        if ch == '\r':
            floc += 1
            ch = fdata[floc]
        assert ch == '\n'
        startstream = floc + 1
        return startstream
    findstream = staticmethod(findstream)

    def expand_deferred(self, source):
        ''' Un-defer all the deferred objects.
        '''
        deferredset = self.deferred
        obj_offsets = self.obj_offsets
        specialget = self.special.get
        indirect_objects = self.indirect_objects
        findstream = self.findstream
        fdata = self.fdata
        DeferredObject = self.DeferredObject
        streams = []
        streamending = 'endstream endobj'.split()

        while deferredset:
            deferred = deferredset.pop()
            key = deferred.key
            offset = obj_offsets[key]

            # Read the object header and validate it
            objnum, gennum = key
            source.setstart(offset)
            objid = source.multiple(3)
            assert int(objid[0]) == objnum, objid
            assert int(objid[1]) == gennum, objid
            assert objid[2] == 'obj', objid

            # Read the object, and call special code if it starts
            # an array or dictionary
            obj = source.next()
            func = specialget(obj)
            if func is not None:
                obj = func(source)

            # Replace occurences of the deferred object
            # with the real thing.
            deferred.value = obj
            indirect_objects[key] = obj
            for parent, index in deferred.usedby:
                parent[index] = obj

            # Mark the object as indirect, and
            # add it to the list of streams if it starts a stream
            obj.indirect = True
            startstream = findstream(obj, source)
            if startstream is not None:
                streams.append((obj, startstream))

        # Once we've read ALL the indirect objects, including
        # stream lengths, we can update the stream objects with
        # the stream information.
        for obj, startstream in streams:
            endstream = startstream + int(obj.Length)
            obj._stream = fdata[startstream:endstream]
            source.setstart(endstream)
            assert source.multiple(2) == streamending

        # We created the top dict by merging other dicts,
        # so now we need to fix up the indirect objects there.
        for key, obj in list(self.iteritems()):
            if isinstance(obj, DeferredObject):
                self[key] = obj.value

    def findxref(fdata):
        ''' Find the cross reference section at the end of a file
        '''
        startloc = fdata.rfind('startxref')
        xrefinfo = list(PdfTokens(fdata, startloc, False))
        assert len(xrefinfo) == 3, xrefinfo
        assert xrefinfo[0] == 'startxref', xrefinfo[0]
        assert xrefinfo[1].isdigit(), xrefinfo[1]
        assert xrefinfo[2].rstrip() == '%%EOF', repr(xrefinfo[2])
        return startloc, PdfTokens(fdata, int(xrefinfo[1]))
    findxref = staticmethod(findxref)

    def parsexref(self, source, int=int, range=range):
        ''' Parse (one of) the cross-reference file section(s)
        '''
        fdata = self.fdata
        setdefault = self.obj_offsets.setdefault
        next = source.next
        tok = next()
        assert tok == 'xref', tok
        while 1:
            tok = next()
            if tok == 'trailer':
                break
            startobj = int(tok)
            for objnum in range(startobj, startobj + int(next())):
                offset = int(next())
                generation = int(next())
                if next() == 'n':
                    setdefault((objnum, generation), offset)

    def readpages(self, node, pagename=PdfName.Page, pagesname=PdfName.Pages):
        # PDFs can have arbitrarily nested Pages/Page
        # dictionary structures.
        if node.Type == pagename:
            return [node]
        assert node.Type == pagesname, node.Type
        result = []
        for node in node.Kids:
            result.extend(self.readpages(node))
        return result

    def __init__(self, fname=None, fdata=None, decompress=True):

        if fname is not None:
            assert fdata is None
            # Allow reading preexisting streams like pyPdf
            if hasattr(fname, 'read'):
                fdata = fname.read()
            else:
                f = open(fname, 'rb')
                fdata = f.read()
                f.close()

        assert fdata is not None
        fdata = fdata.rstrip('\00')
        self.private.fdata = fdata

        self.private.indirect_objects = {}
        self.private.special = {'<<': self.readdict, '[': self.readarray}
        self.private.deferred = deferred = set()
        self.private.obj_offsets = {}

        startloc, source = self.findxref(fdata)
        while 1:
            # Loop through all the cross-reference tables
            self.parsexref(source)
            assert source.next() == '<<'
            # Do not overwrite preexisting entries
            newdict = self.readdict(source).copy()
            newdict.update(self)
            self.update(newdict)

            # Loop if any previously-written tables.
            token = source.next()
            assert token == 'startxref' # and source.floc > startloc, (token, source.floc, startloc)
            if self.Prev is None:
                break
            source.setstart(int(self.Prev))
            self.Prev = None

        self.expand_deferred(source)
        self.private.pages = self.readpages(self.Root.Pages)
        if decompress:
            self.uncompress()

        # For compatibility with pyPdf
        self.numPages = len(self.pages)


    # For compatibility with pyPdf
    def getPage(self, pagenum):
        return self.pages[pagenum]

    def uncompress(self):
        uncompress([x[1] for x in self.indirect_objects.itervalues()])
