#cell 0
import glob
import os
import sys
import re
import datetime
from dateutil import parser as dateutil_parser
import numpy as np
import concurrent
import pyarrow as pa
import pathlib
from timeit import default_timer as timer
import multiprocessing
from pyqstrat.pq_utils import infer_compression, millis_since_epoch, touch
from pyqstrat.pyqstrat_cpp import TextFileDecompressor, TextFileProcessor, PrintBadLineHandler, PriceQtyMissingDataHandler
from pyqstrat.pyqstrat_cpp import WriterCreator, ArrowWriterCreator, Aggregator, Schema, Record, Writer

from typing import Sequence, Optional, Mapping, Any, Tuple, Generator, Callable, Iterable, Union
VERBOSE = False

RecordGeneratorType = Union[Iterable[str], Iterable[bytes]]
RecordGeneratorCreatorType = Callable[[str, str], RecordGeneratorType]
RecordParserType = Callable[[Sequence[str]], Record]
RecordParserCreatorType = Callable[[int, Sequence[str]], RecordParserType]
HeaderParserType = Callable[[str, str], Sequence[str]]
HeaderParserCreatorType = Callable[[RecordGeneratorCreatorType], HeaderParserType]
LineFilterType = Callable[[str], bool]
RecordFilterType = Callable[[Record], bool]
BadLineHandlerType = Callable[[str, Exception], Record]
MissingDataHandlerType = Callable[[Record], None]
FileProcessorType = Callable[[str, Optional[str]], int]
InputFileNameProviderType = Callable[[], Sequence[str]]
WriterCreatorType = Callable[[str, Schema, bool, int], Writer]
OutputFilePrefixMapperType = Callable[[str], str]
BaseDateMapperType = Callable[[str], int]
AggregatorCreatorType = Callable[[WriterCreatorType, str], Sequence[Aggregator]]
FileProcessorCreatorType = Callable[[
    RecordGeneratorType, 
    Optional[LineFilterType], 
    RecordParserType, 
    BadLineHandlerType, 
    Optional[RecordFilterType], 
    MissingDataHandlerType, 
    Sequence[Aggregator]],
    FileProcessorType]


class PathFileNameProvider:
    '''A helper class that, given a pattern such as such as "/tmp/abc*.gz" and an optional include and exclude pattern, 
    returns names of all files that match
    '''
    def __init__(self, path: str, include_pattern: str = None, exclude_pattern: str = None) -> None:
        '''
        Args:
            path: A pattern such as "/tmp/abc*.gz"
            include_pattern: Given a pattern such as "xzy", will return only filenames that contain xyz
            exclude_pattern: Given a pattern such as "_tmp", will exclude all filenames containing _tmp
        '''
        self.path = path
        self.include_pattern = include_pattern
        self.exclude_pattern = exclude_pattern
        
    def __call__(self) -> Sequence[str]:
        '''
        Returns:
            All matching filenames
        '''
        files = glob.glob(self.path)
        if not len(files):
            raise Exception(f'no matching files found with pattern: {self.path}')
        if self.include_pattern:
            files = [file for file in files if self.include_pattern in file]
        if self.exclude_pattern:
            files = [file for file in files if self.exclude_pattern not in file]
        if not len(files):
            raise Exception(f'no matching files for: {self.path} including: {self.include_pattern} excluding: {self.exclude_pattern}')
        return files
    
class SingleDirectoryFileNameMapper:
    '''A helper class that provides a mapping from input filenames to their corresponding output filenames in an output directory.'''
    def __init__(self, output_dir: str) -> None:
        '''
        Args:
            output_dir: The directory where we want to write output files
        '''
        if not os.path.isdir(output_dir): raise Exception(f'{output_dir} does not exist')
        self.output_dir = output_dir

    def __call__(self, input_filepath: str) -> str:
        '''
        Args:
            input_filepath: The input file that we are creating an output file for, e.g. "/home/xzy.gz"
        Returns:
            Output file path for that input.  We take the filename from the input filepath, strip out any extension 
                and prepend the output directory name
        '''
        
        if self.output_dir is None:
            dirname = os.path.dirname(input_filepath)
            dirname = os.path.join(dirname, 'output')
        else:
            dirname = self.output_dir
            
        if not os.path.isdir(dirname): raise Exception(f'{dirname} does not exist')
     
        input_filename = os.path.basename(input_filepath)
        exts = r'\.txt$|\.gz$|\.bzip2$|\.bz$|\.tar$|\.zip$|\.csv$'
        while (re.search(exts, input_filename)):
            input_filename = '.'.join(input_filename.split('.')[:-1])
            if VERBOSE: print(f'got input file: {input_filename}')
        output_prefix = os.path.join(dirname, input_filename)
        return output_prefix

class TextHeaderParser:
    '''
    Parses column headers from a text file containing market data
    '''
    def __init__(self, 
                 record_generator_creator: RecordGeneratorCreatorType, 
                 skip_rows: int = 0, 
                 separator: str = ',', 
                 make_lowercase: bool = True) -> None:
        '''
        Args:
        
            record_generator: A function that takes a filename and its compression type and returns an object
                that we can use to iterate through lines in that file
            skip_rows: Number of rows to skip before starting to read the file.  Default is 0
            separator: Separator for headers.  Defaults to ,
            make_lowercase: Whether to convert headers to lowercase before returning them
        '''
        self.record_generator_creator = record_generator_creator
        self.skip_rows = 0
        self.separator = separator
        self.make_lowercase = make_lowercase
        
    def __call__(self, input_filename: str, compression: str) -> Sequence[str]:
        '''
        Args:
        
        input_filename The file to read
        compression: Compression type, e.g. "gzip", or None if the file is not compressed
        
        Returns:
            Column headers
        '''
        decode_needed = (compression is not None and compression != "")
        
        f = self.record_generator_creator(input_filename, compression)
        headers = None
        for line_num, line in enumerate(f):
            if decode_needed: line = line.decode() # type: ignore # str does not have decode
            if line_num < self.skip_rows: continue
            headers = line.split(self.separator)  # type: ignore  # byte does not have split
            headers = [re.sub('[^A-Za-z0-9 ]+', '', header) for header in headers]
            if len(headers) == 1:
                raise Exception(f'Could not parse headers: {line} with separator: {self.separator}')
            break

        if headers is None: raise Exception('no headers found')
        if self.make_lowercase: headers = [header.lower() for header in headers]
        if VERBOSE: print(f'Found headers: {headers}')
        return headers

        parts = input_filename.split('.')
            
            
def text_file_record_generator_creator(filename: str, compression: str = None) -> RecordGeneratorType:
    '''
    A helper function that returns a generator that we can use to iterate through lines in the input file
    Args:
        filename: The input filename
        compression: The compression type of the input file or None if its not compressed    
    '''
    if compression is None: compression = infer_compression(filename)
    if compression == None or compression == '':
        return open(filename, 'r')
    if compression == 'gzip':
        import gzip
        return gzip.open(filename, 'r')
    if compression == 'bz2':
        import bz2
        return bz2.BZ2File(filename, 'r')
    if compression == 'zip':
        import zipfile
        zf = zipfile.ZipFile(filename, mode = 'r', compression = zipfile.ZIP_DEFLATED)
        zip_infos = zf.infolist()
        zip_names = [zi.filename for zi in zip_infos if not zi.is_dir() ]
        if len(zip_names) == 0: raise ValueError(f'zero files found in ZIP file {filename}')
        return zf.open(zip_names.pop())
    if compression == 'xz':
        import lzma
        return lzma.LZMAFile(filename, 'r')
    raise ValueError(f'unrecognized compression: {compression} for file: {filename}')

def base_date_filename_mapper(input_file_path: str) -> int:
    '''
    A helper function that parses out the date from a filename.  For example, given a file such as "/tmp/spx_2018-08-09", this parses out the 
    date part of the filename and returns milliseconds (no fractions) since the epoch to that date.
    
    Args:
        input_filepath (str): Full path to the input file
    
    Returns:
       int: Milliseconds since unix epoch to the date implied by that file
    
    >>> base_date_filename_mapper("/tmp/spy_1970-1-2_quotes.gz")
    86400000
    '''
    filename = os.path.basename(input_file_path)
    base_date = dateutil_parser.parse(filename, fuzzy=True)
    return round(millis_since_epoch(base_date))

def create_text_file_processor(
    record_generator: RecordGeneratorType, 
    line_filter: Optional[LineFilterType], 
    record_parser: RecordParserType,
    bad_line_handler: BadLineHandlerType, 
    record_filter: Optional[RecordFilterType],
    missing_data_handler: MissingDataHandlerType,
    aggregators : Sequence[Aggregator],
    skip_rows: int = 1) -> FileProcessorType:
    
    return TextFileProcessor(record_generator,
                             line_filter,
                             record_parser,
                             bad_line_handler,
                             record_filter,
                             missing_data_handler,
                             aggregators,
                             skip_rows)

def get_field_indices(field_names: Sequence[str], headers: Sequence[str]) -> Sequence[int]:
    '''
    Helper function to get indices of field names in a list of headers
    
    Args:
        field_names (list of str): The fields we want indices of
        headers (list of str): All headers
        
    Returns:
        list of int: indices of each field name in the headers list
    '''
    field_ids = np.ones(len(field_names), dtype = np.int) * -1
    for i, field_name in enumerate(field_names):
        if field_name not in headers: raise Exception(f'{field_name} not in {headers}')
        field_ids[i] = headers.index(field_name)
    return field_ids

def process_marketdata_file(
    input_filename: str,
    output_file_prefix_mapper: OutputFilePrefixMapperType,
    record_parser_creator: RecordParserCreatorType,
    aggregator_creator: AggregatorCreatorType,
    line_filter: LineFilterType = None, 
    compression: str = None,
    base_date_mapper: BaseDateMapperType = None,
    file_processor_creator: FileProcessorCreatorType = create_text_file_processor,
    header_parser_creator: HeaderParserCreatorType = lambda record_generator_creator :  TextHeaderParser(record_generator_creator),
    header_record_generator: RecordGeneratorCreatorType = text_file_record_generator_creator,
    record_generator: RecordGeneratorType = TextFileDecompressor(),
    bad_line_handler: BadLineHandlerType = PrintBadLineHandler(),
    record_filter: RecordFilterType = None,
    missing_data_handler: MissingDataHandlerType = PriceQtyMissingDataHandler(), 
    writer_creator: WriterCreatorType = ArrowWriterCreator()) -> None:
    
    '''
    Processes a single market data file
    
    Args:
        input_filename :
        output_file_prefix_mapper: A function that takes an input filename and returns the corresponding output filename we want
        record_parser_creator:  A function that takes a date and a list of column names and returns a 
            function that can take a list of fields and return a subclass of Record
        aggregator_creator: A function that takes a writer creator and a output file prefix and returns a list of Aggregators
        line_filter: A function that takes a line and decides whether we want to keep it or discard it.  Defaults to None
        compression: Compression type for the input file.  Defaults to None
        base_date_mapper: A function that takes an input filename and returns the date implied by the filename, 
            represented as millis since epoch.  Defaults to helper :obj:`function base_date_filename_mapper`
        file_processor_creator: A function that returns an object that we can use to iterate through lines in a file.  Defaults to
            helper function :obj:`create_text_file_processor`
        header_record_generator: A function that takes a filename and compression and returns a generator that we can use to get column headers
        record_generator: A function that takes a filename and compression and returns a generator that we 
            can use to iterate through lines in the file
        bad_line_handler (optional): A function that takes a line that we could not parse, and either parses it or does something else
            like recording debugging info, or stopping the processing by raising an exception.  Defaults to helper function 
            :obj:`PrintBadLineHandler`
        record_filter (optional): A function that takes a parsed TradeRecord, QuoteRecord, OpenInterestRecord or OtherRecord and decides whether we
            want to keep it or discard it.  Defaults to None
        missing_data_handler (optional):  A function that takes a parsed TradeRecord, QuoteRecord, OpenInterestRecord or OtherRecord, and decides
            deals with any data that is missing in those records.  For example, 0 for bid could be replaced by NAN.  Defaults to helper function:
            :obj:`price_qty_missing_data_handler`
        writer_creator (optional): A function that takes an output_file_prefix, schema, whether to create a batch id file, and batch_size
            and returns a subclass of :obj:`Writer`.  Defaults to helper function: :obj:`arrow_writer_creator`
    '''
    
    output_file_prefix = output_file_prefix_mapper(input_filename)
    
    base_date = 0
    
    if base_date_mapper is not None: base_date = base_date_mapper(input_filename)
    
    header_parser = header_parser_creator(header_record_generator)
    print(f'starting file: {input_filename}')
    if compression is None or compression == "": compression = infer_compression(input_filename)
    if compression is None: compression = ""  # In C++ don't want virtual function with default argument, so don't allow it here
    headers = header_parser(input_filename, compression)  # type: ignore # cannot be None at this point.  
    
    record_parser = record_parser_creator(base_date, headers)
    
    aggregators = aggregator_creator(writer_creator, output_file_prefix)

    file_processor = file_processor_creator(
        record_generator, 
        line_filter, 
        record_parser, 
        bad_line_handler, 
        record_filter, 
        missing_data_handler,
        aggregators
    )

    start = timer()
    if compression is None: compression = ""
    lines_processed = file_processor(input_filename, compression)
    end = timer()
    duration = round((end - start) * 1000)
    touch(output_file_prefix + '.done')
    print(f"processed: {input_filename} {lines_processed} lines in {duration} milliseconds")
                    
def process_marketdata(
    input_filename_provider: InputFileNameProviderType,
    file_processor: FileProcessorType,
    num_processes: int = None,
    raise_on_error: bool = True) -> None:
    '''
    Top level function to process a set of market data files
    
    Args:
        input_filename_provider: A function that returns a list of filenames (incl path) we need to process.
        file_processor: A function that takes an input filename and processes it, returning number of lines processed. 
        num_processes (int, optional): The number of processes to run to parse these files.  If set to None, we use the number of cores
            present on your machine.  Defaults to None
        raise_on_error (bool, optional): If set, we raise an exception when there is a problem with parsing a file, so we can see a stack
            trace and diagnose the problem.  If not set, we print the error and continue.  Defaults to True
    '''
    
    input_filenames = input_filename_provider()
    if sys.platform in ["win32", "cygwin"] and num_processes is not None and num_processes > 1:
        raise Exception("num_processes > 1 not supported on windows")
     
    if num_processes is None: num_processes = multiprocessing.cpu_count()
        
    if num_processes == 1 or sys.platform in ["win32", "cygwin"]:
        for input_filename in input_filenames:
            try:
                file_processor(input_filename, "")
            except Exception as e:
                new_exc = type(e)(f'Exception: {str(e)}').with_traceback(sys.exc_info()[2])
                if raise_on_error: 
                    raise new_exc
                else: 
                    print(str(new_exc))
                    continue
    else:
        with concurrent.futures.ProcessPoolExecutor(num_processes) as executor:
            fut_filename_map = {}
            for input_filename in input_filenames:
                fut = executor.submit(file_processor, input_filename)
                fut_filename_map[fut] = input_filename
            for fut in concurrent.futures.as_completed(fut_filename_map):
                try:
                    fut.result()
                    if VERBOSE: print(f'done filename: {fut_filename_map[fut]}')
                except Exception as e:
                    new_exc = type(e)(f'Exception: {str(e)}').with_traceback(sys.exc_info()[2])
                    if raise_on_error: 
                        raise new_exc
                    else: 
                        print(str(new_exc))
                        continue

