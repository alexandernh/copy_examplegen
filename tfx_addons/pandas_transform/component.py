# Copyright 2022 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""PandasTransform TFX component.  Feature engineering in TFX using Pandas Dataframes."""

import os

import apache_beam as beam
import pandas as pd
import tensorflow as tf
import tensorflow_data_validation as tfdv
from absl import logging
from packaging.version import Version, parse
from tensorflow_transform.tf_metadata import schema_utils
from tfx import v1 as tfx
from tfx.components.util import tfxio_utils
from tfx.dsl.component.experimental.decorators import component
from tfx.types import artifact_utils
from tfx.types.standard_artifacts import Examples, ExampleStatistics, Schema
from tfx.utils import import_utils, io_utils

if parse(tfx.__version__) >= Version('1.8.0'):
  from tfx.dsl.component.experimental.annotations import BeamComponentParameter
  from tfx.types import system_executions

_TELEMETRY_DESCRIPTORS = ['PandasTransform']


class Arrow2PandasTypes(beam.DoFn):
  """Beam DoFn that converts from the Arrow types that are generated by TFXIO to Pandas types."""
  def __init__(self):
    pass

  # TODO: This does not correctly process features with valence > 1
  def process(self, element, schema=None):
    """Processes each dataframe to change types based on the schema.
    Args:
      element: a Pandas dataframe, converted from an Arrow RecordBatch.
      schema: a Python dict of a schema proto created by SchemaGen.
    Returns:
      Updated dataframe
    """
    if schema is None:
      raise ValueError('Arrow2PandasTypes: Schema is required')

    for key, feature in element.items():
      for idx, _ in enumerate(feature):
        if feature[idx].size > 0:
          feature[idx] = feature[idx][0]
          if schema[key] == 'string':
            feature[idx] = feature[idx].decode('utf8')
        else:
          feature[idx] = None
      feature = feature.astype(schema[key], copy=False)

    yield element


class GetExamples(beam.DoFn):
  """Beam DoFn that created TF.train.Examples from the rows of a dataframe."""
  def __init__(self):
    pass

  def DictToExample(self, row, ptypes):
    """Creates TF.train.Examples containing tf.train.Features from a dataframe
    Args:
      row: A dict containing a row from a dataframe
      ptypes: A dict of the Pandas types for this dataframe
    Returns:
      The corresponding tf.train.Example
    """
    feature = {}
    for key, val in row.items():
      val = [] if pd.isna(val) else [val]
      if ptypes[key] == 'Int64':
        feature[key] = tf.train.Feature(int64_list=tf.train.Int64List(
            value=val))
      elif ptypes[key] in ['float32', 'Float64']:
        feature[key] = tf.train.Feature(float_list=tf.train.FloatList(
            value=val))
      elif ptypes[key] in ['string', 'object']:
        if val != []:
          val = [val[0].encode('utf8')]
        feature[key] = tf.train.Feature(bytes_list=tf.train.BytesList(
            value=val))
      else:
        raise ValueError('DictToExample: Type {} was unhandled'.format(
            ptypes[key]))
    return tf.train.Example(features=tf.train.Features(feature=feature))

  def process(self, df):
    """The Beam DoFn process function.  Yields serialized tf.train.Examples
      Args:
        df: A Pandas dataframe
      Returns:
        Yields serialized tf.train.Examples
    """
    ptypes = df.dtypes.apply(lambda x: x.name).to_dict()
    for row_dict in df.to_dict(orient='records'):
      example = self.DictToExample(row_dict, ptypes)
      yield example.SerializeToString()


if parse(tfx.__version__) >= Version('1.8.0'):

  @component(component_annotation=system_executions.Transform, use_beam=True)
  def PandasTransform(
      transformed_examples: tfx.dsl.components.OutputArtifact[Examples],
      examples: tfx.dsl.components.InputArtifact[Examples] = None,
      schema: tfx.dsl.components.InputArtifact[Schema] = None,
      statistics: tfx.dsl.components.InputArtifact[ExampleStatistics] = None,
      module_file: tfx.dsl.components.Parameter[str] = None,
      beam_pipeline_args: tfx.dsl.components.Parameter[str] = None,
      beam_pipeline: BeamComponentParameter[beam.Pipeline] = None,
  ) -> None:
    """This docstring will be replaced by the shared docstring below"""
    if beam_pipeline_args is not None:
      this_beam_pipeline = beam.Pipeline(argv=beam_pipeline_args.split(' '))
    else:
      this_beam_pipeline = beam_pipeline

    DoPandasTransform(examples=examples,
                      schema=schema,
                      statistics=statistics,
                      transformed_examples=transformed_examples,
                      module_file=module_file,
                      beam_pipeline=this_beam_pipeline)
else:
  logging.info('TFX < 1.8.0')
  logging.info('Beam custom component not supported in this version of TFX.')
  logging.info('You might consider an upgrade.')

  @component
  def PandasTransform(
      transformed_examples: tfx.dsl.components.OutputArtifact[Examples],
      examples: tfx.dsl.components.InputArtifact[Examples] = None,
      schema: tfx.dsl.components.InputArtifact[Schema] = None,
      statistics: tfx.dsl.components.InputArtifact[ExampleStatistics] = None,
      module_file: tfx.dsl.components.Parameter[str] = None,
      beam_pipeline_args: tfx.dsl.components.Parameter[str] = None) -> None:
    """This docstring will be replaced by the shared docstring below"""
    if beam_pipeline_args is not None:
      this_beam_pipeline = beam.Pipeline(argv=beam_pipeline_args.split(' '))
    else:
      this_beam_pipeline = beam.Pipeline()

    DoPandasTransform(examples=examples,
                      schema=schema,
                      statistics=statistics,
                      transformed_examples=transformed_examples,
                      module_file=module_file,
                      beam_pipeline=this_beam_pipeline)


PandasTransform.__doc__ = """The PandasTransform TFX component.
  PandasTransform enables users to perform feature engineering in dataframes, using
  either Pandas, Modin, Numpy, Scikit, or another library which supports dataframes.
  **Important Note:** Because processing is distributed by Beam across a cluster of
  compute nodes, each invocation of PandasTransform will recieve a subset of the data.
  That means that **operations which require full passes over the dataset are not supported.**
  If you require full passes over the dataset, you are encouraged to use TensorFlow
  Transform instead.  However, since PandasTransform is designed to receive a dict of the
  summary statistics which are created by StatisticsGen (or other tooling) then you
  can often use those values to avoid making a full pass over the data.
  **Important Note:** Unlike TensorFlow Transform, PandasTransform does not create a
  preprocessing TensorFlow graph to be prepended to your trained model.  That means
  that you **MUST** perform the equivalent preprocessing in your serving client or
  a similar location in your system.

  Args:
    examples: A TFX input channel containing a dataset artifact
    schema: A TFX input channel containing a schema artifact
    statistics: A TFX input channel containing a statistics artifact
    transformed_examples: A TFX output channel which will be used to output the resulting
    dataset artifact
    module_file: A component parameter containing a file path to a Python file which
    contains the user code, in a function named 'preprocessing_fn'.
    beam_pipeline_args: A string with the argv options for creating a Beam pipeline.
    Note that this is a string, not a list.  It will be split on spaces to create
    a list. If running TFX >= 1.8.0, if beam_pipeline_args are specified they will
    override the pipeline beam args.


  Returns:
    The resulting dataset artifact after processing by the user code.

  Raises:
    ImportError - When the module file is not found.

  Example:
    from tfx_addons.pandas_transform import PandasTransform

    module_file = 'my_module_file.py'
    %%writefile {module_file} # Assuming a Jupyter notebook, otherwise a file
        import numpy as np

        def preprocessing_fn(df, schema, statistics):
        def zscore(x, stats=None):
            if x.isnull()[0] or stats[x.name]['std_dev'] == 0.0:
            return x
            else:
            y = (x - stats[x.name]['mean']) / stats[x.name]['std_dev']
            return y.astype(x.dtype) # to maintain type consistency
            
        numeric_cols = df.select_dtypes(include=[np.number]).columns
        return df[numeric_cols].apply(zscore, stats=statistics)

    _beam_pipeline_args = [
      '--direct_running_mode=in_memory',
      '--direct_num_workers=1']

    panda = PandasTransform(
        examples=example_gen.outputs['examples'],
        schema=schema_gen.outputs['schema'],
        statistics=statistics_gen.outputs['statistics'],
        module_file = os.path.abspath(module_file),
        beam_pipeline_args=' '.join(_beam_pipeline_args)
        )
    context.run(panda, enable_cache=False) # Assuming interactive context
  """


def DoPandasTransform(
    transformed_examples: tfx.dsl.components.OutputArtifact[Examples],
    examples: tfx.dsl.components.InputArtifact[Examples],
    schema: tfx.dsl.components.InputArtifact[Schema],
    statistics: tfx.dsl.components.InputArtifact[ExampleStatistics],
    module_file: tfx.dsl.components.Parameter[str],
    beam_pipeline: beam.pipeline.Pipeline):
  """The function where the actual transforms are done, for both signatures."""
  local_module_file = io_utils.ensure_local(module_file)
  if not os.path.exists(local_module_file):
    raise ImportError(
        f'DoPandasTransform: Module file not found: {module_file}')
  elif examples is None:
    raise ValueError('DoPandasTransform: examples cannot be None')
  elif schema is None:
    raise ValueError('DoPandasTransform: schema cannot be None')
  elif statistics is None:
    raise ValueError('DoPandasTransform: statistics cannot be None')
  elif beam_pipeline is None:
    raise ValueError('DoPandasTransform: beam_pipeline cannot be None')

  def GetFeatureStats(view, feature_name):
    return view.get_feature(tfdv.types.FeaturePath([feature_name])).proto()

  def GetRawFeatureSpec(schema):
    return schema_utils.schema_as_feature_spec(schema).feature_spec

  def WrapUserCode(df, schema=None, statistics=None):
    yield user_code(df, schema, statistics)

  # Get input splits
  input_examples = artifact_utils.get_single_instance([examples])
  examples_split_names = artifact_utils.decode_split_names(
      input_examples.split_names)

  # Init output splits
  transformed_examples = artifact_utils.get_single_instance(
      [transformed_examples])
  transformed_examples.split_names = artifact_utils.encode_split_names(
      examples_split_names)
  for split in examples_split_names:
    os.mkdir(os.path.join(transformed_examples.uri, 'Split-{}'.format(split)))

  # Get the preprocessing_fn from the module file
  user_code = import_utils.import_func_from_source(module_file,
                                                   'preprocessing_fn')

  # Get statistics
  stats_artifact = artifact_utils.get_single_instance([statistics])
  stats_uri = io_utils.get_only_uri_in_dir(
      artifact_utils.get_split_uri([stats_artifact], 'train'))
  stats = tfdv.load_stats_binary(stats_uri)
  stats_view = tfdv.utils.stats_util.DatasetListView(stats).get_default_slice()

  # Get the schema
  schema_file = io_utils.get_only_uri_in_dir(
      artifact_utils.get_single_uri([schema]))
  schema_reader = io_utils.SchemaReader()
  tf_schema = schema_reader.read(schema_file)
  fspec = GetRawFeatureSpec(tf_schema)

  # Convert the statistics and schema to dictionaries
  schema_dict = {}
  schema_dict['domains'] = {}
  stats_dict = {}
  for key, _ in fspec.items():
    if fspec[key].dtype.name == 'int64':
      dtype = 'Int64'
    else:
      dtype = fspec[key].dtype.name
    schema_dict[key] = dtype
    feature = tfdv.utils.schema_util.get_feature(tf_schema, key)
    if tfdv.utils.schema_util.is_categorical_feature(feature):
      schema_dict['domains'][key] = list(
          tfdv.utils.schema_util.get_domain(tf_schema, key).value)

    if dtype in ['Int64', 'float32']:
      feature = GetFeatureStats(stats_view, key)
      stats_dict[key] = {
          'min': feature.num_stats.min,
          'max': feature.num_stats.max,
          'mean': feature.num_stats.mean,
          'median': feature.num_stats.median,
          'std_dev': feature.num_stats.std_dev,
          'max_num_values': feature.num_stats.common_stats.max_num_values
      }

  # Init TFXIO
  tfxio_factory = tfxio_utils.get_tfxio_factory_from_artifact(
      examples=[examples],
      schema=tf_schema,
      telemetry_descriptors=_TELEMETRY_DESCRIPTORS)

  split_and_tfxio = []
  for split in artifact_utils.decode_split_names(examples.split_names):
    split_dir = artifact_utils.get_split_uri([examples], split)
    this_tfxio = tfxio_factory(io_utils.all_files_pattern(split_dir))
    split_and_tfxio.append((split, this_tfxio))

  # Run each split in a Beam pipeline, invoking user code
  # TODO: Replace beam.io.WriteToTFRecord with TFXIO write
  with beam_pipeline:
    for split, tfxio in split_and_tfxio:
      output_split_dir = artifact_utils.get_split_uri([transformed_examples],
                                                      split)
      output_path = os.path.join(output_split_dir, 'data_tfrecord')

      _ = (beam_pipeline
           | 'TFXIORead[{}]'.format(split) >> tfxio.BeamSource()
           | 'Map2Pandas[{}]'.format(split) >>
           beam.Map(lambda record_batch: record_batch.to_pandas())
           | 'Arrow2PandasTypes[{}]'.format(split) >> beam.ParDo(
               Arrow2PandasTypes(), schema=schema_dict)
           | 'UserCode[{}]'.format(split) >> beam.ParDo(
               WrapUserCode, schema=schema_dict, statistics=stats_dict)
           | 'GetExamples[{}]'.format(split) >> beam.ParDo(GetExamples())
           | 'Write[{}]'.format(split) >> beam.io.WriteToTFRecord(
               output_path, file_name_suffix='.gz'))
