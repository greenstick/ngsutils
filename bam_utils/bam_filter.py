#!/usr/bin/env python
"""
Given a BAM file, this script will only allow reads that meet filtering
criteria to be written to output. The output is another BAM file with the
reads not matching the criteria removed.

Note: this does not adjust tag values reflecting any filtering. (for example:
      if a read mapped to two locations (IH:i:2), and one was removed by
      filtering, the IH:i tag would still read IH:i:2).

Currently, the available filters are:
    -length minval             Remove reads that are smaller than {minval}
    -mapped                    Keep only mapped reads
    -mask bitmask              Remove reads that match the mask (base 10/hex)

    -mismatch num              # mismatches or indels
                               indel always counts as 1 regardless of length
                               (requires NM tag in reads)
    -mismatch_ref num ref.fa   # mismatches or indel - looks up mismatches
                               directly in a ref FASTA file (if NM not
                               available)
    -noqcfail                  Remove reads that have the 0x200 flag set
    -nosecondary               Remove reads that have the 0x100 flag set


    -exclude ref:start-end     Remove reads in this region (1-based start)
    -excludebed file.bed       Remove reads that are in any of the regions
                               from the given BED file

    -include ref:start-end     Remove reads NOT in the region (can only be one)
    -includebed file.bed       Remove reads that are NOT any of the regions
                               from the given BED file
                               Note: If this is a large dataset, use
                               "bamutils extract" instead.

    -whitelist fname           Remove reads that aren't on this list (by name)
    -blacklist fname           Remove reads that are on this list (by name)
                                 These lists can be whitespace-delimited with
                                 the read name as the first column.

    -eq  tag_name value
    -lt  tag_name value
    -lte tag_name value
    -gt  tag_name value
    -gte tag_name value

    Where tag_name should be the full name, plus the type eg: AS:i

Common tags to filter by:
    AS:i    Alignment score
    NM:i    Edit distance (each indel counts as many as it's length)
    IH:i    Number of alignments

"""

import os
import sys
import pysam
from support.eta import ETA


def usage():
    print __doc__
    print """
Usage: bamutils filter in.bam out.bam {-failed out.txt} criteria...

Options:
  -failed fname    A text file containing the read names of all reads
                   that were removed with filtering

Example:
bamutils filter filename.bam output.bam -mapped -gte AS:i 1000

This will remove all unmapped reads, as well as any reads that have an AS:i
value less than 1000.
"""
    sys.exit(1)

bam_cigar = ['M', 'I', 'D', 'N', 'S', 'H', 'P']


class Blacklist(object):
    def __init__(self, fname):
        self.fname = fname
        self.notallowed = []
        with open(fname) as f:
            for line in f:
                self.notallowed.append(line.strip().split()[0])

    def filter(self, bam, read):
        return read.qname not in self.notallowed

    def __repr__(self):
        return 'Blacklist: %s' % (self.fname)


class Whitelist(object):
    def __init__(self, fname):
        self.fname = fname
        self.allowed = []
        with open(fname) as f:
            for line in f:
                self.allowed.append(line.strip().split()[0])

    def filter(self, bam, read):
        return read.qname in self.allowed

    def __repr__(self):
        return 'Whitelist: %s' % (self.fname)


class IncludeRegion(object):
    _excludes = []
    _last = None

    def __init__(self, region):
        IncludeRegion._excludes.append(ExcludeRegion(region))
        # self.excl = ExcludeRegion(region)

    def filter(self, bam, read):
        if read == IncludeRegion._last:
            return True

        IncludeRegion._last = read

        for excl in IncludeRegion._excludes:
            if not excl.filter(bam, read):
                return True
        return False

        # return not self.excl.filter(bam,read)
    def __repr__(self):
        return 'Including: %s' % (self.excl.region)


class IncludeBED(object):
    def __init__(self, fname):
        self.excl = ExcludeBED(fname)

    def filter(self, bam, read):
        return not self.excl.filter(bam, read)

    def __repr__(self):
        return 'Including from BED: %s' % (self.excl.fname)


class ExcludeRegion(object):
    def __init__(self, region):
        self.region = region
        spl = region.split(':')
        self.chrom = spl[0]
        se = [int(x) for x in spl[1].split('-')]
        self.start = se[0] - 1
        self.end = se[1]

    def filter(self, bam, read):
        if not read.is_unmapped:
            if bam.getrname(read.rname) == self.chrom:
                if self.start <= read.pos <= self.end:
                    return False
                if self.start <= read.aend <= self.end:
                    return False
        return True

    def __repr__(self):
        return 'Excluding: %s' % (self.region)


class ExcludeBED(object):
    def __init__(self, fname):
        self.regions = []
        self.fname = fname
        with open(fname) as f:
            for line in f:
                if not line:
                    continue
                if line[0] == '#':
                    continue
                cols = line.strip().split('\t')
                self.regions.append((cols[0], int(cols[1]), int(cols[2])))

    def filter(self, bam, read):
        if not read.is_unmapped:
            for chrom, start, end in self.regions:
                if bam.getrname(read.rname) == chrom:
                    if start <= read.pos <= end:
                        return False
                    if start <= read.aend <= end:
                        return False
        return True

    def __repr__(self):
        return 'Excluding from BED: %s' % (self.fname)


class Mismatch(object):
    def __init__(self, num):
        self.num = int(num)

    def filter(self, bam, read):
        if read.is_unmapped:
            return False

        inserts = 0
        deletions = 0
        indels = 0
        edits = int(read.opt('NM'))
        #
        # NM counts the length of indels
        # We really just care about *if* there is an indel, not the size
        #

        for op, length in read.cigar:
            if op == 1:
                inserts += length
                indels += 1
            elif op == 2:
                deletions += length
                indels += 1

        mismatches = edits - inserts - deletions + indels

        if mismatches > self.num:
            return False

        return True

    def __repr__(self):
        return '%s mismatch%s in %s' % (self.num, '' if self.num == 1 else 'es', os.path.basename(self.refname))


class MismatchRef(object):
    def __init__(self, num, refname):
        self.num = int(num)
        self.refname = refname

        if not os.path.exists('%s.fai' % refname):
            pysam.faidx(refname)

        self.ref = pysam.Fastafile(refname)

    def filter(self, bam, read):
        if read.is_unmapped:
            return False
        chrom = bam.getrname(read.rname)
        start = read.pos
        #end = read.aend

        edits = 0
        ref_pos = 0
        read_pos = 0

        for op, length in read.cigar:
            if op == 1:
                edits += 1
                read_pos += length
            elif op == 2:
                edits += 1
                ref_pos += length
            elif op == 3:
                ref_pos += length
            elif op == 0:
                refseq = self.ref.fetch(chrom, start + ref_pos, start + ref_pos + length)
                for refbase, readbase in zip(refseq, read.seq[read_pos:read_pos + length]):
                    if refbase.upper() != readbase.upper():
                        edits += 1
                ref_pos += length
                read_pos += length

            if edits > self.num:
                return False
        return True

    def __repr__(self):
        return '%s mismatch%s in %s' % (self.num, '' if self.num == 1 else 'es', os.path.basename(self.refname))


class Mapped(object):
    def __init__(self):
        pass

    def filter(self, bam, read):
        if read.is_paired and (read.is_unmapped or read.mate_is_unmapped):
            return False
        elif read.is_unmapped:
            return False
        return True

    def __repr__(self):
        return 'is mapped'


class MaskFlag(object):
    def __init__(self, value):
        if value[0:2] == '0x':
            self.flag = int(value, 16)
        else:
            self.flag = int(value)

    def __repr__(self):
        return "Doesn't match flag: %s" % self.flag

    def filter(self, bam, read):
        return (read.flag & self.flag) == 0


class SecondaryFlag(object):
    def __repr__(self):
        return "no 0x100 (secondary) flag"

    def filter(self, bam, read):
        return not read.is_secondary


class ReadLength(object):
    def __init__(self, minval):
        self.minval = int(minval)

    def __repr__(self):
        return "read length min: %s" % self.minval

    def filter(self, bam, read):
        return len(read.seq) >= self.minval


class QCFailFlag(object):
    def __repr__(self):
        return "no 0x200 (qcfail) flag"

    def filter(self, bam, read):
        return not read.is_qcfail


class _TagCompare(object):
    def __init__(self, tag, value):
        self.args = '%s %s' % (tag, value)
        if ':' in tag:
            self.tag = tag[0:tag.find(':')]
        else:
            self.tag = tag

        if tag[-1] == 'i':
            self.value = int(value)
        elif tag[-1] == 'f':
            self.value = float(value)
        else:
            self.value = value

    def __repr__(self):
        return "%s %s %s" % (self.tag, self.__class__.op, self.value)


class TagLessThan(_TagCompare):
    op = '<'

    def filter(self, bam, read):
        for name, value in read.tags:
            if name == self.tag and value < self.value:
                return True
        return False


class TagLessThanEquals(_TagCompare):
    op = '<='

    def filter(self, bam, read):
        for name, value in read.tags:
            if name == self.tag and value <= self.value:
                return True
        return False


class TagGreaterThan(_TagCompare):
    op = '>'

    def filter(self, bam, read):
        for name, value in read.tags:
            if name == self.tag and value > self.value:
                return True
        return False


class TagGreaterThanEquals(_TagCompare):
    op = '>='

    def filter(self, bam, read):
        for name, value in read.tags:
            if name == self.tag and value > self.value:
                return True
        return False


class TagEquals(_TagCompare):
    op = '='

    def filter(self, bam, read):
        for name, value in read.tags:
            if name == self.tag and value == self.value:
                return True
        return False

_criteria = {
    'mapped': Mapped,
    'noqcfail': QCFailFlag,
    'nosecondary': SecondaryFlag,
    'mask': MaskFlag,
    'lt': TagLessThan,
    'gt': TagGreaterThan,
    'lte': TagLessThanEquals,
    'gte': TagGreaterThanEquals,
    'eq': TagEquals,
    'mismatch': Mismatch,
    'mismatch_ref': MismatchRef,
    'exclude': ExcludeRegion,
    'excludebed': ExcludeBED,
    'include': IncludeRegion,
    'includebed': IncludeBED,
    'whitelist': Whitelist,
    'blacklist': Blacklist,
    'length': ReadLength
}


def bam_filter(infile, outfile, criteria, failedfile=None, verbose=False):
    if verbose:
        sys.stderr.write('Input file  : %s\n' % infile)
        sys.stderr.write('Output file : %s\n' % outfile)
        if failedfile:
            sys.stderr.write('Failed reads: %s\n' % failedfile)
        sys.stderr.write('Criteria:\n')
        for criterion in criteria:
            sys.stderr.write('    %s\n' % criterion)

        sys.stderr.write('\n')

    bamfile = pysam.Samfile(infile, "rb")
    outfile = pysam.Samfile(outfile, "wb", template=bamfile)
    if failedfile:
        failed_out = open(failedfile, 'w')
    else:
        failed_out = None
    eta = ETA(0, bamfile=bamfile)

    passed = 0
    failed = 0

    for read in bamfile:
        eta.print_status(extra="kept:%s, failed:%s" % (passed, failed), bam_pos=(read.rname, read.pos))
        p = True

        for criterion in criteria:
            if not criterion.filter(bamfile, read):
                p = False
                failed += 1
                if failed_out:
                    failed_out.write('%s\n' % read.qname)
                #outfile.write(read_to_unmapped(read))
                break
        if p:
            passed += 1
            outfile.write(read)

    eta.done()
    bamfile.close()
    outfile.close()
    if failed_out:
        failed_out.close()
    sys.stdout.write("%s kept\n%s failed\n" % (passed, failed))


def read_to_unmapped(read):
    '''
    Example unmapped read
    2358_2045_1839    |  4 | *                             |  0    |  0   | *                  | *  |  0 |  0 | GGACGAGAAGGAGTATTTCTCCGAGAACACATTCACGGAGAGTCTAACTC           | 0QT\VNO^IIRJKXTIHIKTY\STZZ[XOJKPWLHJQQQ^XPQYIIRQII           | PG:Z:bfast   | AS:i:-                     | NH:i:0   | IH:i:1   | HI:i:1                                     | CS:Z:T10213222020221330022203222011113021130222212230122             | CQ:Z:019=2+><%@.'>52%)'15?90<7;:5+(-49('-7-<>5-@5%<.2%=            | CM:i:0   | XA:i:4

    Example mapped read:
    2216_1487_198     |  16 | chr11:19915291-19925392      |  5531 |   12 | 24M2D26M           | *  |  0 |  0 | TCTGTCTGGGTGCAGTAGCTATACGTAATCCCAGCACTTTGGGAGGCCAA           | 1C8FZ`M""""""WZ""%"#\I;"`R@D]``ZZ``\QKX\Y]````IK^`           | PG:Z:bfast   | AS:i:1150   | NM:i:2   | NH:i:10   | IH:i:1   | HI:i:1   | MD:Z:24^CT26                   | CS:Z:T00103022001002113210023031213331122121223321221122             | CQ:Z:A8=%;AB<;97<3/9:>>3>@?5&18@-%;866:94=14:=?,%?:7&)1            | CM:i:9   | XA:i:4   | XE:Z:-------23322---2-11----2----------------------------

    '''
    # TODO: rewrite read properties to unmapped state
    #       if colorspace: convert CS to NT directly
    #       remove excess tags

    read.is_unmapped = True
    read.rname = None
    read.pos = 0
    read.mapq = 0
    return read

if __name__ == '__main__':
    infile = None
    outfile = None
    failed = None
    criteria = []

    crit_args = []
    last = None
    verbose = False
    fail = False

    for arg in sys.argv[1:]:
        if last == '-failed':
            failed = arg
            last = None
        elif arg == '-h':
            usage()
        elif arg == '-failed':
            last = arg
        elif arg == '-v':
            verbose = True
        elif not infile and os.path.exists(arg):
            infile = arg
        elif not outfile:
            outfile = arg
        elif arg[0] == '-':
            if not arg[1:] in _criteria:
                print "Unknown criterion: %s" % arg
                fail = True
            if crit_args:
                criteria.append(_criteria[crit_args[0][1:]](*crit_args[1:]))
            crit_args = [arg, ]
        elif crit_args:
            crit_args.append(arg)
        else:
            print "Unknown argument: %s" % arg
            fail = True

    if crit_args:
        criteria.append(_criteria[crit_args[0][1:]](*crit_args[1:]))

    if fail or not infile or not outfile or not criteria:
        if not infile and not outfile and not criteria:
            usage()

        if not infile:
            print "Missing: input bamfile"
        if not outfile:
            print "Missing: output bamfile"
        if not criteria:
            print "Missing: filtering criteria"
        usage()
    else:
        bam_filter(infile, outfile, criteria, failed, verbose)
