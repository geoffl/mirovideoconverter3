import json
import logging
import os
import re
import shutil

from mvc import resources, settings, utils
from mvc.utils import hms_to_seconds

from mvc.qtfaststart import processor
from mvc.qtfaststart.exceptions import FastStartException

logger = logging.getLogger(__name__)

NON_WORD_CHARS = re.compile(r"[^a-zA-Z0-9]+")

class ConverterInfo(object):
    media_type = None
    bitrate = None
    extension = None

    def __init__(self, name):
        self.name = name
        self.identifier = NON_WORD_CHARS.sub("", name).lower()

    def get_executable(self):
        raise NotImplementedError

    def get_arguments(self, video, output):
        raise NotImplementedError

    def get_output_filename(self, video):
        basename = os.path.basename(video.filename)
        name, ext = os.path.splitext(basename)
        if ext and ext[0] == '.':
            ext = ext[1:]
        extension = self.extension if self.extension else ext
        return '%s.%s.%s' % (name, self.identifier, extension)

    def get_output_size_guess(self, video):
        if not self.bitrate or not video.duration:
            return None
        if video.duration:
            return self.bitrate * video.duration / 8

    def finalize(self, temp_output, output):
        err = None
        needs_remove = False
        if self.media_type == 'format' and self.extension == 'mp4':
            needs_remove = True
            logging.debug('generic mp4 format detected.  '
                          'Running qtfaststart...')
            try:
                processor.process(temp_output, output)
            except FastStartException:
                logging.exception('qtfaststart: exception occurred')
                err = EnvironmentError('qtfaststart exception')
        else:
            try:
                shutil.move(temp_output, output)
            except EnvironmentError, e:
                needs_remove = True
                err = e
        # If it didn't work for some reason try to clean up the stale stuff.
        # And if that doesn't work ... just log, and re-raise the original
        # error.
        if needs_remove:
            try:
                os.remove(temp_output)
            except EnvironmentError, e:
                logging.error('finalize(): cannot remove stale file %r',
                              temp_output)
                if err:
                    logging.error('finalize(): removal was in response to '
                                  'error: %s', str(err))
                    raise err

    def process_status_line(self, line):
        raise NotImplementedError

class FFmpegConverterInfoBase(ConverterInfo):
    DURATION_RE = re.compile(r'\W*Duration: (\d\d):(\d\d):(\d\d)\.(\d\d)'
                             '(, start:.*)?(, bitrate:.*)?')
    PROGRESS_RE = re.compile(r'(?:frame=.* fps=.* q=.* )?size=.* time=(.*) '
                             'bitrate=(.*)')
    LAST_PROGRESS_RE = re.compile(r'frame=.* fps=.* q=.* Lsize=.* time=(.*) '
                                  'bitrate=(.*)')

    extension = None

    def get_executable(self):
        return settings.get_ffmpeg_executable_path()

    def get_arguments(self, video, output):
        extra_args = settings.customize_ffmpeg_parameters(
          self.get_extra_arguments(video, output))
        return (['-i', video.filename, '-strict', 'experimental'] +
                extra_args + [output])

    def get_extra_arguments(self, video, output):
        raise NotImplementedError

    @staticmethod
    def _check_for_errors(line):
        if line.startswith('Unknown'):
            return line
        if line.startswith("Error"):
            if not line.startswith("Error while decoding stream"):
                return line

    @classmethod
    def process_status_line(klass, video, line):
        error = klass._check_for_errors(line)
        if error:
            return {'finished': True, 'error': error}

        match = klass.DURATION_RE.match(line)
        if match is not None:
            hours, minutes, seconds, centi = [
                int(m) for m in match.groups()[:4]]
            return {'duration': hms_to_seconds(hours, minutes,
                                               seconds + 0.01 * centi)}

        match = klass.PROGRESS_RE.match(line)
        if match is not None:
            t = match.group(1)
            if ':' in t:
                hours, minutes, seconds = [float(m) for m in t.split(':')[:3]]
                return {'progress': hms_to_seconds(hours, minutes, seconds)}
            else:
                return {'progress': float(t)}

        match = klass.LAST_PROGRESS_RE.match(line)
        if match is not None:
            return {'finished': True}


class FFmpegConverterInfo(FFmpegConverterInfoBase):

    parameters = None

    def __init__(self, name, width=None, height=None, dont_upsize=True):
        self.width, self.height = width, height
        self.dont_upsize = dont_upsize
        ConverterInfo.__init__(self, name)

    def get_extra_arguments(self, video, output):
        if self.parameters is None:
            raise NotImplementedError
        width, height = utils.rescale_video((video.width, video.height),
                                            (self.width, self.height),
                                            dont_upsize=self.dont_upsize)
        ssize = '%ix%i' % (width, height)
        return self.parameters.format(
            ssize=ssize,
            input=video.filename,
            output=output).split()

class ConverterManager(object):
    def __init__(self):
        self.converters = {}
        # converter -> brand reverse map.  XXX: this code, really, really sucks
        # and not very scalable.
        self.brand_rmap = {}
        self.brand_map = {}

    def add_converter(self, converter):
        self.converters[converter.identifier] = converter

    def startup(self):
        self.load_simple_converters()
        self.load_converters(resources.converter_scripts())

    def brand_to_converters(self, brand):
        try:
            return self.brand_map[brand]
        except KeyError:
            return None

    def converter_to_brand(self, converter):
        try:
            return self.brand_rmap[converter]
        except KeyError:
            return None

    def load_simple_converters(self):
        from mvc import basicconverters
        for converter in basicconverters.converters:
            if isinstance(converter, tuple):
                brand, realconverters = converter
                for realconverter in realconverters:
                    self.brand_rmap[realconverter] = brand
                    self.brand_map.setdefault(brand, []).append(realconverter)
                    self.add_converter(realconverter)
            else:
                self.brand_rmap[converter] = None
                self.brand_map.setdefault(None, []).append(converter)
                self.add_converter(converter)

    def load_converters(self, converters):
        for converter_file in converters:
            global_dict = {}
            execfile(converter_file, global_dict)
            if 'converters' in global_dict:
                for converter in global_dict['converters']:
                    if isinstance(converter, tuple):
                        brand, realconverters = converter
                        for realconverter in realconverters:
                            self.brand_rmap[realconverter] = brand
                            self.brand_map.setdefault(brand, []).append(realconverter)
                            self.add_converter(realconverter)
                    else:
                        self.brand_rmap[converter] = None
                        self.brand_map.setdefault(None, []).append(converter)
                        self.add_converter(converter)
                logger.info('load_converters: loaded %i from %r',
                            len(global_dict['converters']),
                            converter_file)

    def list_converters(self):
        return self.converters.values()

    def get_by_id(self, id_):
        return self.converters[id_]
