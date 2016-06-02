# -*- coding: utf-8 -*-
#
# © 2016 Krux Digital, Inc.
#

#
# Standard libraries
#

from __future__ import absolute_import
from abc import ABCMeta, abstractmethod
from datetime import datetime
import uuid

#
# Third party libraries
#

import troposphere
import botocore.exceptions

#
# Internal libraries
#

from krux.logging import get_logger
from krux.stats import get_stats
from krux.cli import get_parser, get_group
from krux_boto.boto import Boto, Boto3, add_boto_cli_arguments
from krux_s3.s3 import S3, add_s3_cli_arguments


NAME = 'krux-troposphere'
TEMP_S3_BUCKET = 'krux-temp'
TEMP_S3_REGION = 'us-east-1'


def get_troposphere(args=None, logger=None, stats=None):
    """
    Return a usable Troposphere object without creating a class around it.

    In the context of a krux.cli (or similar) interface the 'args', 'logger'
    and 'stats' objects should already be present. If you don't have them,
    however, we'll attempt to provide usable ones for the Troposphere setup.

    (If you omit the add_troposphere_cli_arguments() call during other cli setup,
    the Boto object will still work, but its cli options won't show up in
    --help output)

    (This also handles instantiating a Boto3 object on its own.)
    """
    if not args:
        parser = get_parser()
        add_troposphere_cli_arguments(parser)
        args = parser.parse_args()

    if not logger:
        logger = get_logger(name=NAME)

    if not stats:
        stats = get_stats(prefix=NAME)

    boto3 = Boto3(
        log_level=args.boto_log_level,
        access_key=args.boto_access_key,
        secret_key=args.boto_secret_key,
        region=args.boto_region,
        logger=logger,
        stats=stats,
    )
    boto = Boto(
        log_level=args.boto_log_level,
        access_key=args.boto_access_key,
        secret_key=args.boto_secret_key,
        # This boto is for S3 upload and is using a constant region,
        # matching the TEMP_S3_BUCKET
        region=TEMP_S3_REGION,
        logger=logger,
        stats=stats,
    )
    s3 = S3(
        boto=boto,
        logger=logger,
        stats=stats,
    )
    return Troposphere(
        boto=boto3,
        s3=s3,
        logger=logger,
        stats=stats,
    )


def add_troposphere_cli_arguments(parser, include_boto_arguments=True):
    """
    Utility function for adding Troposphere specific CLI arguments.
    """
    if include_boto_arguments:
        # GOTCHA: Since many modules use krux_boto, the krux_boto's CLI arguments can be included twice,
        # causing an error. This creates a way to circumvent that.

        # Add all the boto arguments
        add_boto_cli_arguments(parser)

    add_s3_cli_arguments(parser, False)

    # Add those specific to the application
    group = get_group(parser, NAME)


class Troposphere(object):
    """
    A manager to handle all Troposphere / Cloud Formation related functions.
    Each instance is locked to a connection to a designated region (self.boto.cli_region).
    """

    STACK_NOT_EXIST_ERROR_MSG = 'Stack with id {stack_name} does not exist'
    NO_UPDATE_ERROR_MSG = 'No updates are to be performed.'
    _S3_KEY_TEMPLATE = '{name}/{stack_name}-{datestamp}'
    _DATESTAMP_TEMPLATE = '{year}{month}{date}-{hour}{minute}{second}'
    # S3 link expires after an hour
    _S3_URL_EXPIRY = 3600

    def __init__(
        self,
        boto,
        s3,
        logger=None,
        stats=None,
    ):
        """
        :param boto: :py:class:`krux_boto.boto.Boto3` Boto3 object used to connect to Cloud Formation
        :param logger: :py:class:`logging.Logger` Logger, recommended to be obtained using krux.cli.Application
        :param stats: :py:class:`kruxstatsd.StatsClient` Stats, recommended to be obtained using krux.cli.Application
        """
        # Private variables, not to be used outside this module
        self._name = NAME
        self._logger = logger or get_logger(self._name)
        self._stats = stats or get_stats(prefix=self._name)

        if not isinstance(boto, Boto3):
            raise NotImplementedError('Currently krux_troposphere.troposphere.Troposphere only supports krux_boto.boto.Boto3')

        self._s3 = s3

        self._cf = boto.client('cloudformation')
        self.template = troposphere.Template()

    def _is_stack_exists(self, stack_name):
        """
        Check if the given Cloud Formation stack exists

        GOTCHA: There is no simple way to check the existence of a stack.
        The template for the stack is fetched and if an expected exception occur (Unable to find stack),
        then the stack is deemed not existing.

        :param stack_name: :py:class:`str` Name of the stack to check
        """
        try:
            # See if we can get a template for this
            self._cf.get_template(StackName=stack_name)
            # The template was successfully retrieved; the stack exists
            return True
        except botocore.exceptions.ClientError as err:
            if self.STACK_NOT_EXIST_ERROR_MSG.format(stack_name=stack_name) == err.response.get('Error', {}).get('Message', ''):
                # The template was not retrieved; the stack does not exists
                return False

            # Unknown error. Raise again.
            raise

    @staticmethod
    def _get_timestamp(datetime):
        return Troposphere._DATESTAMP_TEMPLATE.format(
            year=datetime.year,
            month=format(datetime.month, '02'),
            date=format(datetime.day, '02'),
            hour=format(datetime.hour, '02'),
            minute=format(datetime.minute, '02'),
            second=format(datetime.second, '02'),
        )

    def save(self, stack_name):
        """
        Saves the template to the given Cloud Formation stack.

        The method internally checks whether the stack exists and either creates or updates the stack
        with the template in this object.

        :param stack_name: :py:class:`str` Name of the stack to check
        """
        key = self._S3_KEY_TEMPLATE.format(
            name=self._name,
            stack_name=stack_name,
            datestamp=Troposphere._get_timestamp(datetime.utcnow()),
        )
        s3_file = self._s3.create_key(bucket_name=TEMP_S3_BUCKET, key=key, str_content=self.template.to_json())

        if self._is_stack_exists(stack_name):
            try:
                self._cf.update_stack(
                    StackName=stack_name,
                    TemplateURL=s3_file.generate_url(self._S3_URL_EXPIRY)
                )
            except botocore.exceptions.ClientError as err:
                if self.NO_UPDATE_ERROR_MSG == err.response.get('Error', {}).get('Message', ''):
                    # Nothing was updated. Ignore this error and move on.
                    return

                # Unknown error. Raise again.
                raise
        else:
            self._cf.create_stack(
                StackName=stack_name,
                TemplateURL=s3_file.generate_url(self._S3_URL_EXPIRY)
            )