# SPDX-License-Identifier: GPL-3.0-or-later
import base64
from contextlib import contextmanager
import functools
import hashlib
import inspect
import json
import logging
import os
import re
import subprocess

from iib.workers.dogpile_cache import (
    create_dogpile_region,
    dogpile_cache,
    skopeo_inspect_should_use_cache,
)
from operator_manifest.operator import ImageName

from iib.exceptions import IIBError
from iib.workers.config import get_worker_config
from iib.workers.api_utils import set_request_state

log = logging.getLogger(__name__)
dogpile_cache_region = create_dogpile_region()


def get_binary_image_from_config(ocp_version, distribution_scope, binary_image_config={}):
    """
    Determine the binary image to be used to build the index image.

    :param str ocp_version: the ocp_version label value of the index image.
    :param str distribution_scope: the distribution_scope label value of the index image.
    :param dict binary_image_config: the dict of config required to identify the appropriate
        ``binary_image`` to use.
    :return: pull specification of the binary_image to be used for this build.
    :rtype: str
    :raises IIBError: when the config value for the ocp_version and distribution_scope is missing.
    """
    binary_image = binary_image_config.get(distribution_scope, {}).get(ocp_version, None)
    if not binary_image:
        raise IIBError(
            'IIB does not have a configured binary_image for'
            f' distribution_scope : {distribution_scope} and ocp_version: {ocp_version}.'
            ' Please specify a binary_image value in the request.'
        )

    return binary_image


class RequestConfig:
    """Request config abstract class.

    :param str _binary_image:  the pull specification of the container image
                          where the opm binary gets copied from.
    :param str distribution_scope: the scope for distribution
        of the index image, defaults to ``None``.
    :param str source_from_index: the pull specification of the container image
        containing the index that will be used
        as a base of the merged index image.
    :param str target_index: the pull specification of the container image
        containing the index whose new data will be added
        to the merged index image.
    :param dict binary_image_config: the dict of config required to
        identify the appropriate ``binary_image`` to use.
    """

    _attrs = ["_binary_image", "distribution_scope", "binary_image_config"]
    __slots__ = _attrs

    def __init__(self, **kwargs):
        """
        Request config __init__.

        Do not use this directly, use subclasses instead.
        :Keyword Arguments:
            See `_attrs` to check accepted keyword arguments.
        """
        for key in self.__slots__:
            setattr(self, key, None)
        for key, val in kwargs.items():
            setattr(self, key, kwargs[key])

    def __eq__(self, other):
        if type(self) == type(other) and [getattr(self, x) for x in self.__slots__] == [
            getattr(self, x) for x in self.__slots__
        ]:
            return True
        return False

    def binary_image(self, index_info, distribution_scope):
        """Get binary image based on self configuration, index image info and distribution scope."""
        if not self._binary_image:
            binary_image_ocp_version = index_info['ocp_version']
            return get_binary_image_from_config(
                binary_image_ocp_version, distribution_scope, self.binary_image_config
            )
        return self._binary_image


class RequestConfigAddRm(RequestConfig):
    """Request config for add and remove operations.

    :param str overwrite_from_index_token: the token used for overwriting the input
        ``from_index`` image. This is required for
        non-privileged users to use ``overwrite_from_index``.
        The format of the token must be
        in the format "user:password".
    :param str from_index: the pull specification of the container image
        containing the index that the index image build
        will be based from.
    :param list add_arches: the list of arches to build in addition to the
        arches ``from_index`` is currently built for;
        if ``from_index`` is ``None``, then this is used as the list of arches
        to build the index image for
    :param list bundles: the list of bundles to create the
        bundle mapping on the request
    """

    _attrs = RequestConfig._attrs + [
        "overwrite_from_index_token",
        "from_index",
        "add_arches",
        "bundles",
        "operators",
    ]
    __slots__ = _attrs


class RequestConfigMerge(RequestConfig):
    """Request config for merge operation.

    :param str overwrite_target_index_token:  auth token used to pus index image
        when overwrite is set.
    :param str source_from_index: the pull specification of the container image
        containing the index that will be used
        as a base of the merged index image.
    :param str target_index: the pull specification of the container image
        containing the index whose new data will be added
        to the merged index image.
    """

    _attrs = RequestConfig._attrs + [
        "source_from_index",
        "target_index",
        "overwrite_target_index_token",
    ]

    __slots__ = _attrs


def deprecate_bundles(
    bundles,
    base_dir,
    binary_image,
    from_index,
    overwrite_target_index_token=None,
    container_tool=None,
):
    """
    Deprecate the specified bundles from the index image.

    Only Dockerfile is created, no build is performed.

    :param list bundles: pull specifications of bundles to deprecate.
    :param str base_dir: base directory where operation files will be located.
    :param str binary_image: binary image to be used by the new index image.
    :param str from_index: index image, from which the bundles will be deprecated.
    :param str overwrite_target_index_token: the token used for overwriting the input
        ``from_index`` image. This is required for non-privileged users to use
        ``overwrite_target_index``. The format of the token must be in the format "user:password".
    :param str container_tool: the container tool to be used to operate on the index image
    """
    cmd = [
        'opm',
        'index',
        'deprecatetruncate',
        '--generate',
        '--binary-image',
        binary_image,
        '--from-index',
        from_index,
        '--bundles',
        ','.join(bundles),
    ]
    if container_tool:
        cmd.append('--container-tool')
        cmd.append(container_tool)
    with set_registry_token(overwrite_target_index_token, from_index):
        run_cmd(cmd, {'cwd': base_dir}, exc_msg='Failed to deprecate the bundles')


def get_bundles_from_deprecation_list(bundles, deprecation_list):
    """
    Get a list of to-be-deprecated bundles based on the data from the deprecation list.

    :param list bundles: list of bundles pull spec to apply the filter on.
    :param list deprecation_list: list of deprecated bundle pull specifications.
    :return: bundles which are to be deprecated.
    :rtype: list
    """
    resolved_deprecation_list = get_resolved_bundles(deprecation_list)
    deprecate_bundles = []
    for bundle in bundles:
        if bundle in resolved_deprecation_list:
            deprecate_bundles.append(bundle)

    log.info(
        'Bundles that will be deprecated from the index image: %s', ', '.join(deprecate_bundles)
    )
    return deprecate_bundles


def get_resolved_bundles(bundles):
    """
    Get the pull specification of the bundle images using their digests.

    Determine if the pull spec refers to a manifest list.
    If so, simply use the digest of the first item in the manifest list.
    If not a manifest list, it must be a v2s2 image manifest and should be used as it is.

    :param list bundles: the list of bundle images to be resolved.
    :return: the list of bundle images resolved to their digests.
    :rtype: list
    :raises IIBError: if unable to resolve a bundle image.
    """
    log.info('Resolving bundles %s', ', '.join(bundles))
    resolved_bundles = set()
    for bundle_pull_spec in bundles:
        skopeo_raw = skopeo_inspect(
            f'docker://{bundle_pull_spec}', '--raw', require_media_type=True
        )
        if (
            skopeo_raw.get('mediaType')
            == 'application/vnd.docker.distribution.manifest.list.v2+json'
        ):
            # Get the digest of the first item in the manifest list
            digest = skopeo_raw['manifests'][0]['digest']
            name = _get_container_image_name(bundle_pull_spec)
            resolved_bundles.add(f'{name}@{digest}')
        elif (
            skopeo_raw.get('mediaType') == 'application/vnd.docker.distribution.manifest.v2+json'
            and skopeo_raw.get('schemaVersion') == 2
        ):
            resolved_bundles.add(get_resolved_image(bundle_pull_spec))
        else:
            error_msg = (
                f'The pull specification of {bundle_pull_spec} is neither '
                f'a v2 manifest list nor a v2s2 manifest. Type {skopeo_raw.get("mediaType")}'
                f' and schema version {skopeo_raw.get("schemaVersion")} is not supported by IIB.'
            )
            raise IIBError(error_msg)

    return list(resolved_bundles)


def _get_container_image_name(pull_spec):
    """
    Get the container image name from a pull specification.

    :param str pull_spec: the pull spec to analyze
    :return: the container image name
    """
    if '@' in pull_spec:
        return pull_spec.split('@', 1)[0]
    else:
        return pull_spec.rsplit(':', 1)[0]


def get_resolved_image(pull_spec):
    """
    Get the pull specification of the container image using its digest.

    :param str pull_spec: the pull specification of the container image to resolve
    :return: the resolved pull specification
    :rtype: str
    """
    log.debug('Resolving %s', pull_spec)
    name = _get_container_image_name(pull_spec)
    skopeo_output = skopeo_inspect(f'docker://{pull_spec}', '--raw', return_json=False)
    if json.loads(skopeo_output).get('schemaVersion') == 2:
        raw_digest = hashlib.sha256(skopeo_output.encode('utf-8')).hexdigest()
        digest = f'sha256:{raw_digest}'
    else:
        # Schema 1 is not a stable format. The contents of the manifest may change slightly
        # between requests causing a different digest to be computed. Instead, let's leverage
        # skopeo's own logic for determining the digest in this case. In the future, we
        # may want to use skopeo in all cases, but this will have significant performance
        # issues until https://github.com/containers/skopeo/issues/785
        digest = skopeo_inspect(f'docker://{pull_spec}')['Digest']
    pull_spec_resolved = f'{name}@{digest}'
    log.debug('%s resolved to %s', pull_spec, pull_spec_resolved)
    return pull_spec_resolved


def get_image_labels(pull_spec):
    """
    Get the labels from the image.

    :param list<str> labels: the labels to get
    :return: the dictionary of the labels on the image
    :rtype: dict
    """
    if pull_spec.startswith('docker://'):
        full_pull_spec = pull_spec
    else:
        full_pull_spec = f'docker://{pull_spec}'
    log.debug('Getting the labels from %s', full_pull_spec)
    return skopeo_inspect(full_pull_spec, '--config').get('config', {}).get('Labels', {})


def retry(attempts=get_worker_config().iib_total_attempts, wait_on=Exception, logger=None):
    """
    Retry a section of code until success or max attempts are reached.

    :param int attempts: the total number of attempts to make before erroring out
    :param Exception wait_on: the exception on encountering which the function will be retried
    :param logging logger: the logger to log the messages on
    :raises IIBError: if the maximum attempts are reached
    """

    def wrapper(function):
        @functools.wraps(function)
        def inner(*args, **kwargs):
            remaining_attempts = attempts
            while True:
                try:
                    return function(*args, **kwargs)
                except wait_on as e:
                    remaining_attempts -= 1
                    if remaining_attempts <= 0:
                        if logger is not None:
                            logger.exception(
                                'The maximum number of attempts (%s) have failed', attempts
                            )
                        raise
                    if logger is not None:
                        logger.warning(
                            'Exception %r raised from %r.  Retrying now',
                            e,
                            f'{function.__module__}.{function.__name__}',
                        )

        return inner

    return wrapper


def reset_docker_config():
    """Create a symlink from ``iib_docker_config_template`` to ``~/.docker/config.json``."""
    conf = get_worker_config()
    docker_config_path = os.path.join(os.path.expanduser('~'), '.docker', 'config.json')

    try:
        log.debug('Removing the Docker config at %s', docker_config_path)
        os.remove(docker_config_path)
    except FileNotFoundError:
        pass

    if os.path.exists(conf.iib_docker_config_template):
        log.debug(
            'Creating a symlink from %s to %s', conf.iib_docker_config_template, docker_config_path
        )
        os.symlink(conf.iib_docker_config_template, docker_config_path)


@contextmanager
def set_registry_token(token, container_image):
    """
    Configure authentication to the registry that ``container_image`` is from.

    This context manager will reset the authentication to the way it was after it exits. If
    ``token`` is falsy, this context manager will do nothing.

    :param str token: the token in the format of ``username:password``
    :param str container_image: the pull specification of the container image to parse to determine
        the registry this token is for.
    :return: None
    :rtype: None
    """
    if not token:
        log.debug(
            'Not changing the Docker configuration since no overwrite_from_index_token was provided'
        )
        yield

        return

    if not container_image:
        log.debug('Not changing the Docker configuration since no from_index was provided')
        yield

        return

    registry = ImageName.parse(container_image).registry
    encoded_token = base64.b64encode(token.encode('utf-8')).decode('utf-8')
    registry_auths = {'auths': {registry: {'auth': encoded_token}}}
    with set_registry_auths(registry_auths):
        yield


@contextmanager
def set_registry_auths(registry_auths):
    """
    Configure authentication to the registry with provided dockerconfig.json.

    This context manager will reset the authentication to the way it was after it exits. If
    ``registry_auths`` is falsy, this context manager will do nothing.
    :param dict registry_auths: dockerconfig.json auth only information to private registries

    :return: None
    :rtype: None
    """
    if not registry_auths:
        log.debug('Not changing the Docker configuration since no registry_auths were provided')
        yield

        return

    docker_config_path = os.path.join(os.path.expanduser('~'), '.docker', 'config.json')
    try:
        log.debug('Removing the Docker config symlink at %s', docker_config_path)
        try:
            os.remove(docker_config_path)
        except FileNotFoundError:
            log.debug('The Docker config symlink at %s does not exist', docker_config_path)

        conf = get_worker_config()
        if os.path.exists(conf.iib_docker_config_template):
            with open(conf.iib_docker_config_template, 'r') as f:
                docker_config = json.load(f)
        else:
            docker_config = {}

        registries = list(registry_auths.get('auths', {}).keys())
        log.debug(
            'Setting the override token for the registries %s in the Docker config', registries
        )

        docker_config.setdefault('auths', {})
        docker_config['auths'].update(registry_auths.get('auths', {}))
        with open(docker_config_path, 'w') as f:
            json.dump(docker_config, f)

        yield
    finally:
        reset_docker_config()


@retry(wait_on=IIBError, logger=log)
@dogpile_cache(
    dogpile_region=dogpile_cache_region, should_use_cache_fn=skopeo_inspect_should_use_cache
)
def skopeo_inspect(*args, return_json=True, require_media_type=False):
    """
    Wrap the ``skopeo inspect`` command.

    :param args: any arguments to pass to ``skopeo inspect``
    :param bool return_json: if ``True``, the output will be parsed as JSON and returned
    :param bool require_media_type: if ``True``, ``mediaType`` will be checked in the output
        and it will be ignored when ``return_json`` is ``False``
    :return: a dictionary of the JSON output from the skopeo inspect command
    :rtype: dict
    :raises IIBError: if the command fails and if ``mediaType`` is not found in the output while
        ``require_media_type`` is ``True``
    """
    exc_msg = None
    for arg in args:
        if arg.startswith('docker://'):
            exc_msg = f'Failed to inspect {arg}. Make sure it exists and is accessible to IIB.'
            break

    skopeo_timeout = get_worker_config().iib_skopeo_timeout
    cmd = ['skopeo', '--command-timeout', skopeo_timeout, 'inspect'] + list(args)
    output = run_cmd(cmd, exc_msg=exc_msg)
    if not return_json:
        return output

    json_output = json.loads(output)

    if require_media_type and not json_output.get('mediaType'):
        raise IIBError('mediaType not found')
    return json_output


@retry(wait_on=IIBError, logger=log)
def podman_pull(*args):
    """
    Wrap the ``podman pull`` command.

    :param args: any arguments to pass to ``podman pull``
    :raises IIBError: if the command fails
    """
    run_cmd(
        ['podman', 'pull'] + list(args),
        exc_msg=f'Failed to pull the container image {" ".join(args)}',
    )


def run_cmd(cmd, params=None, exc_msg=None):
    """
    Run the given command with the provided parameters.

    :param iter cmd: iterable representing the command to be executed
    :param dict params: keyword parameters for command execution
    :param str exc_msg: an optional exception message when the command fails
    :return: the command output
    :rtype: str
    :raises IIBError: if the command fails
    """
    exc_msg = exc_msg or 'An unexpected error occurred'
    if not params:
        params = {}
    params.setdefault('universal_newlines', True)
    params.setdefault('encoding', 'utf-8')
    params.setdefault('stderr', subprocess.PIPE)
    params.setdefault('stdout', subprocess.PIPE)

    log.debug('Running the command "%s"', ' '.join(cmd))
    response = subprocess.run(cmd, **params)

    if response.returncode != 0:
        log.error('The command "%s" failed with: %s', ' '.join(cmd), response.stderr)
        if cmd[0] == 'opm':
            # Capture the error message right before the help display
            regex = r'^(?:Error: )(.+)$'
            # Start from the last log message since the failure occurs near the bottom
            for msg in reversed(response.stderr.splitlines()):
                match = re.match(regex, msg)
                if match:
                    raise IIBError(f'{exc_msg.rstrip(".")}: {match.groups()[0]}')

        raise IIBError(exc_msg)

    return response.stdout


def request_logger(func):
    """
    Log messages relevant to the current request to a dedicated file.

    If ``iib_request_logs_dir`` is set, a temporary log handler is added before the decorated
    function is invoked. It's then removed once the decorated function completes execution.

    If ``iib_request_logs_dir`` is not set, the temporary log handler will not be added.

    :param function func: the function to be decorated. The function must take the ``request_id``
        parameter.
    :return: the decorated function
    :rtype: function
    """
    worker_config = get_worker_config()
    log_dir = worker_config.iib_request_logs_dir
    log_level = worker_config.iib_request_logs_level
    log_format = worker_config.iib_request_logs_format

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        request_log_handler = None
        if log_dir:
            log_formatter = logging.Formatter(log_format)
            request_id = _get_function_arg_value('request_id', func, args, kwargs)
            if not request_id:
                raise IIBError(f'Unable to get "request_id" from {func.__name__}')

            log_file_path = os.path.join(log_dir, f'{request_id}.log')
            request_log_handler = logging.FileHandler(log_file_path)
            request_log_handler.setLevel(log_level)
            request_log_handler.setFormatter(log_formatter)
            os.chmod(log_file_path, 0o775)
            logger = logging.getLogger()
            logger.addHandler(request_log_handler)
        try:
            return func(*args, **kwargs)
        finally:
            if request_log_handler:
                logger.removeHandler(request_log_handler)

    return wrapper


def _get_function_arg_value(arg_name, func, args, kwargs):
    """Return the value of the given argument name."""
    original_func = func
    while getattr(original_func, '__wrapped__', None):
        original_func = original_func.__wrapped__
    argspec = inspect.getfullargspec(original_func).args

    arg_index = argspec.index(arg_name)
    arg_value = kwargs.get(arg_name, None)
    if arg_value is None and len(args) > arg_index:
        arg_value = args[arg_index]
    return arg_value


def chmod_recursively(dir_path, dir_mode, file_mode):
    """Change file mode bits recursively.

    :param str dir_path: the path to the starting directory to apply the file mode bits
    :param dir_mode int: the mode, as defined in the stat module, to apply to directories
    :param file_mode int: the mode, as defined in the stat module, to apply to files
    """
    for dirpath, dirnames, filenames in os.walk(dir_path):
        os.chmod(dirpath, dir_mode)
        for filename in filenames:
            file_path = os.path.join(dirpath, filename)
            # As per the man pages:
            #   On Linux, the permissions of an ordinary symbolic link are not used in any
            #   operations; the permissions are always 0777, and can't be changed.
            #   - https://www.man7.org/linux/man-pages/man7/symlink.7.html
            #
            # The python docs state that islink will only return True if the symlink points
            # to an existing file.
            #   - https://docs.python.org/3/library/os.path.html#os.path.islink
            # To completely ignore attempting to set permissions on a symlink, first verify the
            # file exists.
            if not os.path.exists(file_path) or os.path.islink(file_path):
                continue
            os.chmod(file_path, file_mode)


def gather_index_image_arches(build_request_config, index_image_infos):
    """Gather architectures from build_request_config and provided index image.

    :param RequestConfig build_request_config: build request configuration
    :param dict index_image_infos: dict with index image infos returned
        by `get_all_index_images_info`
    :return: set of architecture of all index images
    :rtype: set
    """
    arches = set(
        (build_request_config.add_arches if hasattr(build_request_config, 'add_arches') else [])
        or []
    )
    for info in index_image_infos.values():
        arches |= set(info['arches'])

    if not arches:
        raise IIBError('No arches were provided to build the index image')
    return arches


def get_image_arches(pull_spec):
    """
    Get the architectures this image was built for.

    :param str pull_spec: the pull specification to a v2 manifest list
    :return: a set of architectures of the container images contained in the manifest list
    :rtype: set
    :raises IIBError: if the pull specification is not a v2 manifest list
    """
    log.debug('Get the available arches for %s', pull_spec)
    skopeo_raw = skopeo_inspect(f'docker://{pull_spec}', '--raw')
    arches = set()
    if skopeo_raw.get('mediaType') == 'application/vnd.docker.distribution.manifest.list.v2+json':
        for manifest in skopeo_raw['manifests']:
            arches.add(manifest['platform']['architecture'])
    elif skopeo_raw.get('mediaType') == 'application/vnd.docker.distribution.manifest.v2+json':
        skopeo_out = skopeo_inspect(f'docker://{pull_spec}', '--config')
        arches.add(skopeo_out['architecture'])
    else:
        raise IIBError(
            f'The pull specification of {pull_spec} is neither a v2 manifest list nor a v2 manifest'
        )

    return arches


def get_index_image_info(overwrite_from_index_token, from_index=None, default_ocp_version='v4.5'):
    """Get arches, resolved pull specification and ocp_version for the index image.

    :param str overwrite_from_index_token: the token used for overwriting the input
        ``from_index`` image. This is required for non-privileged users to use
        ``overwrite_from_index``. The format of the token must be in the format "user:password".
    :param str from_index: the pull specification of the index image to be resolved.
    :param str default_ocp_version: default ocp_version to use if index image pull_spec is absent.
    :return: dictionary of resolved index image pull spec, set of arches, default ocp_version and
        resolved_distribution_scope
    :rtype: dict
    """
    result = {
        'resolved_from_index': None,
        'ocp_version': default_ocp_version,
        'arches': set(),
        'resolved_distribution_scope': 'prod',
    }
    if not from_index:
        return result

    with set_registry_token(overwrite_from_index_token, from_index):
        from_index_resolved = get_resolved_image(from_index)
        result['arches'] = get_image_arches(from_index_resolved)
        result['ocp_version'] = (
            get_image_label(from_index_resolved, 'com.redhat.index.delivery.version') or 'v4.5'
        )
        result['resolved_distribution_scope'] = (
            get_image_label(from_index_resolved, 'com.redhat.index.delivery.distribution_scope')
            or 'prod'
        )
        result['resolved_from_index'] = from_index_resolved
    return result


def get_all_index_images_info(build_request_config, index_version_map):
    """Get image info of all images in version map.

    :param RequestConfig build_request_config: build request configuration
    :param list index_version_map: list of tuples with (index_name, index_ocp_version)
    :return: dictionary with inex image information obtained from `get_index_image_info`
    :rtype: dict
    """
    infos = {}
    for (index, version) in index_version_map:
        if not hasattr(build_request_config, index):
            from_index = None
        else:
            from_index = getattr(build_request_config, index)

        infos[index] = get_index_image_info(
            build_request_config.overwrite_from_index_token
            if hasattr(build_request_config, 'overwrite_from_index_token')
            else build_request_config.overwrite_target_index_token,
            from_index=from_index,
            default_ocp_version=version,
        )
    return infos


def get_image_label(pull_spec, label):
    """
    Get a specific label from the container image.

    :param str pull_spec: pull spec of the image
    :param str label: the label to get
    :return: the label on the container image or None
    :rtype: str
    """
    log.debug('Getting the label of %s from %s', label, pull_spec)
    return get_image_labels(pull_spec).get(label)


def verify_labels(bundles):
    """
    Verify that the required labels are set on the input bundles.

    :param list bundles: a list of strings representing the pull specifications of the bundles to
        add to the index image being built.
    :raises IIBError: if one of the bundles does not have the correct label value.
    """
    conf = get_worker_config()
    if not conf['iib_required_labels']:
        return

    for bundle in bundles:
        labels = get_image_labels(bundle)
        for label, value in conf['iib_required_labels'].items():
            if labels.get(label) != value:
                raise IIBError(f'The bundle {bundle} does not have the label {label}={value}')


def _validate_distribution_scope(resolved_distribution_scope, distribution_scope):
    """
    Validate distribution scope is allowed to be updated.

    :param str resolved_distribution_scope: the distribution_scope that the index is for.
    :param str distribution_scope: the distribution scope that has been requested for
        the index image.
    :return: the valid distribution scope
    :rtype: str
    :raises IIBError: if the ``resolved_distribution_scope`` is of lesser scope than
        ``distribution_scope``
    """
    if not distribution_scope:
        return resolved_distribution_scope

    scopes = ["dev", "stage", "prod"]
    # Make sure the request isn't regressing the distribution scope
    if scopes.index(distribution_scope) > scopes.index(resolved_distribution_scope):
        raise IIBError(
            f'Cannot set "distribution_scope" to {distribution_scope} because from index is'
            f' already set to {resolved_distribution_scope}'
        )
    return distribution_scope


def prepare_request_for_build(request_id, build_request_config):
    """Prepare the request for the index image build.

    All information that was retrieved and/or calculated for the next steps in the build are
    returned as a dictionary.
    This function was created so that code didn't need to be duplicated for the ``add`` and ``rm``
    request types.
    :param RequestConfig build_request_config: build request configuration
    :return: a dictionary with the keys: arches, binary_image_resolved, from_index_resolved, and
    ocp_version.
    :rtype: dict
    :raises IIBError: if the container image resolution fails or the architectures couldn't be
    detected.
    """
    bundles = None
    if hasattr(build_request_config, "bundles"):
        bundles = build_request_config.bundles

    if bundles is None:
        bundles = []

    set_request_state(request_id, 'in_progress', 'Resolving the container images')

    # Use v4.5 as default version
    index_info = get_all_index_images_info(
        build_request_config,
        [("from_index", "v4.5"), ("source_from_index", "v4.5"), ("target_index", "v4.6")],
    )
    arches = gather_index_image_arches(build_request_config, index_info)
    if not arches:
        raise IIBError('No arches were provided to build the index image')

    arches_str = ', '.join(sorted(arches))
    log.debug('Set to build the index image for the following arches: %s', arches_str)

    # Use the distribution_scope of the from_index as the resolved distribution scope for `Add`,
    # and 'Rm' requests, but use the distribution_scope of the target_index as the resolved
    # distribution scope for `merge-index-image` requests.
    resolved_distribution_scope = index_info['from_index']['resolved_distribution_scope']
    if (
        hasattr(build_request_config, "source_from_index")
        and build_request_config.source_from_index
    ):
        resolved_distribution_scope = index_info['target_index']['resolved_distribution_scope']

    distribution_scope = _validate_distribution_scope(
        resolved_distribution_scope, build_request_config.distribution_scope
    )

    binary_image = build_request_config.binary_image(index_info['from_index'], distribution_scope)

    binary_image_resolved = get_resolved_image(binary_image)
    binary_image_arches = get_image_arches(binary_image_resolved)

    if not arches.issubset(binary_image_arches):
        raise IIBError(
            'The binary image is not available for the following arches: {}'.format(
                ', '.join(sorted(arches - binary_image_arches))
            )
        )

    bundle_mapping = {}
    for bundle in bundles:
        operator = get_image_label(bundle, 'operators.operatorframework.io.bundle.package.v1')
        if operator:
            bundle_mapping.setdefault(operator, []).append(bundle)

    return {
        'arches': arches,
        'binary_image': binary_image,
        'binary_image_resolved': binary_image_resolved,
        'bundle_mapping': bundle_mapping,
        'from_index_resolved': index_info["from_index"]['resolved_from_index'],
        'ocp_version': index_info["from_index"]['ocp_version'],
        'distribution_scope': distribution_scope,
        'source_from_index_resolved': index_info['source_from_index']['resolved_from_index'],
        'source_ocp_version': index_info['source_from_index']['ocp_version'],
        'target_index_resolved': index_info['target_index']['resolved_from_index'],
        'target_ocp_version': index_info['target_index']['ocp_version'],
    }
