# -*- coding: utf-8 -*-
#
# Copyright © 2016 Mark Wolf
#
# This file is part of Xanespy.
#
# Xanespy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Xanespy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Xanespy. If not, see <http://www.gnu.org/licenses/>.

"""Tools for importing X-ray microscopy frames in formats produced by
Xradia instruments."""

import datetime as dt
from collections import namedtuple
import struct
import os
import re
import warnings

from PIL import OleFileIO
import numpy as np
import pytz

import exceptions
from utilities import shape, Pixel


def decode_aps_params(filename):
    """Accept the filename of an XRM file and return sample parameters as
    a dictionary."""
    regex = re.compile(
        '(?P<pos>[a-zA-Z0-9_]+)_xanes(?P<sam>[a-zA-Z0-9_]+)_(?P<E_int>[0-9]+)_(?P<E_dec>[0-9])eV.xrm'
    )
    match = regex.search(filename).groupdict()
    energy = float("{}.{}".format(match['E_int'], match['E_dec']))
    result = {
        'sample_name': match['sam'],
        'position_name': match['pos'],
        'is_background': match['pos'] == 'ref',
        'energy': energy,
    }
    return result

def decode_ssrl_params(filename):
    """Accept the filename of an XRM file and return sample parameters as
    a dictionary."""
    # Beamline 6-2c at SSRL
    ssrl_regex_bg = re.compile(
        'rep(\d{2})_(\d{6})_ref_[0-9]+_([-a-zA-Z0-9_]+)_([0-9.]+)_eV_(\d{3})of(\d{3})\.xrm'
    )
    ssrl_regex_sample = re.compile(
        'rep(\d{2})_[0-9]+_([-a-zA-Z0-9_]+)_([0-9.]+)_eV_(\d{3})of(\d{3}).xrm'
    )
    # Check for background frames
    bg_result = ssrl_regex_bg.search(filename)
    sample_result = ssrl_regex_sample.search(filename)
    if bg_result:
        params = {
            'repetition': int(bg_result.group(1)),
            'date_string': '',
            'sample_name': bg_result.group(3).strip("_"),
            'position_name': '',
            'is_background': True,
            'energy': float(bg_result.group(4)),
        }
    elif sample_result:
        params = {
            'repetition': int(sample_result.group(1)),
            'date_string': '',
            'sample_name': sample_result.group(2).strip("_"),
            'position_name': '',
            'is_background': False,
            'energy': float(sample_result.group(3)),
        }
    else:
        msg = "Could not parse filename {filename} using flavor {flavor}"
        raise exceptions.FilenameParseError(msg.format(filename=filename, flavor='ssrl'))
    return params

# Some of the byte decoding was taken from
# https://github.com/data-exchange/data-exchange/blob/master/xtomo/src/xtomo_reader.py


class XRMFile():
    """Single X-ray micrscopy frame created using XRadia XRM format.

    Arguments
    ---------
    - filename : The path to the .xrm file

    - flavor : The variety of data represented in the xrm file. Valid
      choices are ['ssrl', 'aps', 'aps-old1']. These choices should
      line up with whatever is generated using the scripts in
      beamlines moudles.
    """
    aps_old1_regex = re.compile("(\d{8})_([a-zA-Z0-9_]+)_([a-zA-Z0-9]+)_(\d{4}).xrm")

    def __init__(self, filename, flavor: str):
        self.filename = filename
        self.flavor = flavor
        self.ole_file = OleFileIO.OleFileIO(self.filename)
        # Filename parameters
        params = self.parameters_from_filename()
        self.sample_name = params['sample_name']
        self.position_name = params['position_name']
        self.is_background = params['is_background']

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()

    def __str__(self):
        return os.path.basename(self.filename)

    def __repr__(self):
        return "<XRMFile: '{}'>".format(os.path.basename(self.filename))

    def close(self):
        """Close original XRM (ole) file on disk."""
        self.ole_file.close()

    def parameters_from_filename(self):
        """Determine various metadata from the frames filename (sample name etc)."""
        if self.flavor == 'aps':
            params = decode_aps_params(self.filename)
        elif self.flavor == 'aps-old1':
            # APS beamline 8-BM-B
            result = self.aps_old1_regex.search(self.filename)
            params = {
                'date_string': result.group(1),
                'sample_name': result.group(2),
                'position_name': result.group(3),
                'is_background': result.group(3) == 'bkg',
                'energy': float(result.group(4)),
            }
        elif self.flavor == 'ssrl':
            params = decode_ssrl_params(self.filename)
        else:
            msg = "Unknown flavor for filename: {}"
            raise exceptions.FileFormatError(msg.format(self.filename))
        return params

    def um_per_pixel(self):
        """Describe the size of a pixel in microns. If this is an SSRL frame,
        the pixel size is dependent on energy. For APS frames, the pixel size
        is uniform and assumes a 40µm field-of-view.
        """
        # Based on calibration data from Yijin:
        #   For 9 keV, at binning 2, the pixel size is 35.54 nm.  In our
        #   system, the pixel size is proportional to the X-ray energy.
        #   Say you have 8.5 keV, your pixel size at binning 2 will be
        #   36.39296/9*8.5
        if self.flavor == "ssrl":
            energy = self.energy()
            field_size = 36.39296 * energy / 9000
        else:
            field_size = 40
        num_pixels = max(self.image_data().shape)
        pixel_size = field_size / num_pixels
        return pixel_size

    def ole_value(self, stream, fmt=None):
        """Get arbitrary data from the ole file and convert from bytes."""
        stream_bytes = self.ole_file.openstream(stream).read()
        if fmt is not None:
            stream_value = struct.unpack(fmt, stream_bytes)[0]
        else:
            stream_value = stream_bytes
        return stream_value

    def print_ole(self):
        for l in self.ole_file.listdir():
            if l[0] == 'ImageInfo':
                try:
                    val = self.ole_value(l, '<f')
                except:
                    pass
                else:
                    print(l, ':', val)

    def endtime(self):
        """Retrieve a datetime object representing when this frame was
        finished collecting. Duration is decided by exposure time of the frame
        and the start time."""
        exptime = self.ole_value('ImageInfo/ExpTimes', '<f')
        startime = self.starttime()
        duration = dt.timedelta(seconds=exptime)
        return startime + duration

    def starttime(self):
        """Retrieve a datetime object representing when this frame was
        collected. Timezone is inferred from flavor (eg. ssrl -> california
        time)."""
        # Decode bytestring.
        # (First 16 bytes contain a date and time, not sure about the rest)
        dt_bytes = self.ole_value('ImageInfo/Date')[0:17]
        dt_string = dt_bytes.decode()
        # Determine most likely timezone based on synchrotron location
        if self.flavor == 'ssrl':
            timezone = pytz.timezone('US/Pacific')
        elif self.flavor in ['aps', 'aps-old1']:
            timezone = pytz.timezone('US/Central')
        else:
            # Unknown flavor. Raising here instead of constructor
            # means your forgot to put in the proper timezone.
            msg = "Unknown timezone for flavor '{flavor}'. Assuming UTC"
            warnings.warn(msg.format(flavor=self.flavor))
            timezone = pytz.utc
        # Convert string into datetime object
        fmt = "%m/%d/%y %H:%M:%S"
        timestamp = dt.datetime.strptime(dt_string, fmt).replace(tzinfo=timezone)
        return timestamp

    def energy(self):
        """Beam energy in electronvoltes."""
        # Try reading from file first
        energy = self.ole_value('ImageInfo/Energy', '<f')
        if not energy > 0:
            # if not, read from filename
            re_result = re.search("(\d+\.?\d?)_?eV", self.filename)
            if re_result:
                energy = float(re_result.group(1))
            else:
                msg = "Could not read energy for file {}"
                raise exceptions.FileFormatError(msg.format(self.filename))
        return energy

    def image_data(self):
        """TXM Image frame."""
        # Figure out byte size
        dimensions = self.image_size()
        num_bytes = dimensions.horizontal * dimensions.vertical
        # Determine format string
        image_dtype = self.image_dtype()
        if image_dtype == 'uint16':
            fmt_str = "<{}h".format(num_bytes)
        elif image_dtype == 'float32':
            fmt_str = "<{}f".format(num_bytes)
        # Return decoded image data
        stream = self.ole_file.openstream('ImageData1/Image1')
        img_data = struct.unpack(fmt_str, stream.read())
        img_data = np.reshape(img_data, dimensions)
        return img_data

    # def is_background(self):
    #     """Look at the file name for clues to whether this is a background
    #     frame."""
    #     result = re.search('bkg|_ref_', self.filename)
    #     return bool(result)

    def sample_position(self):
        position = namedtuple('position', ('x', 'y', 'z'))
        x = self.ole_value('ImageInfo/XPosition', '<f')
        y = self.ole_value('ImageInfo/YPosition', '<f')
        z = self.ole_value('ImageInfo/ZPosition', '<f')
        return position(x, y, z)

    def binning(self):
        vertical = self.ole_value('ImageInfo/VerticalalBin', '<L')
        horizontal = self.ole_value('ImageInfo/HorizontalBin', '<L')
        binning = namedtuple('binning', ('horizontal', 'vertical'))
        return binning(horizontal, vertical)

    def image_dtype(self):
        dtypes = {
            5: 'uint16',
            10: 'float32',
        }
        dtype_number = self.ole_value('ImageInfo/DataType', '<1I')
        return dtypes[dtype_number]

    def image_size(self):
        resolution = namedtuple('dimensions', ('horizontal', 'vertical'))
        horizontal = self.ole_value('ImageInfo/ImageWidth', '<I')
        vertical = self.ole_value('ImageInfo/ImageHeight', '<I')
        return resolution(horizontal=horizontal, vertical=vertical)
