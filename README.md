# IIB Image Builder (IIB) Service

A REST API to manage operator index container images (and some bundle images).

IIB is a recursive acronym.  IIB stands for IIB Image Builder.

Note: IIB was originally called "Index Image Build Service" but the name has been changed since
  its scope widened.

Podman 1.9.2+ is required by IIB.

## External Documentation

* [API Documentation](http://release-engineering.github.io/iib)
* [General Documentation](https://iib.readthedocs.io/en/latest/)
* [Python Module Documentation](https://iib.readthedocs.io/en/latest/module_documentation/index.html)

## Coding Standards

The codebase conforms to the style enforced by `flake8` with the following exceptions:

* The maximum line length allowed is 100 characters instead of 80 characters

In addition to `flake8`, docstrings are also enforced by the plugin `flake8-docstrings` with
the following exemptions:

* D100: Missing docstring in public module
* D104: Missing docstring in public package
* D105: Missing docstring in magic method

The format of the docstrings should be in the
[reStructuredText](https://docs.python-guide.org/writing/documentation/#restructuredtext-ref) style
such as:

```python
"""
Get the IIB build request from the REST API.

:param int request_id: the ID of the IIB request
:return: the request
:rtype: dict
:raises IIBError: if the HTTP request fails
"""
```

Additionally, `black` is used to enforce other coding standards with the following exceptions:

* Single quotes are used instead of double quotes

To verify that your code meets these standards, you may run `tox -e black,flake8`.

## Running the Unit Tests

The testing environment is managed by [tox](https://tox.readthedocs.io/en/latest/). Simply run
`tox` and all the linting and unit tests will run.

If you'd like to run a specific unit test, you can do the following:

```bash
tox -e py37 tests/test_web/test_api_v1.py::test_add_bundle_invalid_param
```

## Development Environment

[docker-compose](https://docs.docker.com/compose/) is the supported mechanism for setting up a
development environment. This will automatically run the following containers:

* **iib-api** - the IIB REST API. This is accessible at [http://localhost:8080](http://localhost:8080).
* **iib-worker** - the IIB Celery worker.
* **rabbitmq** - the RabbitMQ instance for communicating between the API and the worker. The
  management UI is accessible at [http://localhost:8081](http://localhost:8081). The username is
  `iib` and the password is `iib`.
* **db** - the Postgresql database used by the IIB REST API.
* **registry** - the Docker Registry where the worker pushes its build index images to. This is
  accessible at docker://localhost:8443.
* **message-broker** - the Apache ActiveMQ instance for publishing messages for external consumers.
  The web console is accessible at [http://localhost:8161/admin](http://localhost:8161/admin). The
  username is `admin` and the password is `admin`. The docker-compose environment is configured for
  IIB to publish AMQP 1.0 messages to the Apache ActiveMQ instance at the destinations
  `topic://VirtualTopic.eng.iib.batch.state` and `topic://VirtualTopic.eng.iib.build.state`.

It's recommended to use the wrapper targets in the `Makefile` for pre-requisites. Simply run `make`
to view available targets.

The Flask application will automatically reload if there is a change in the codebase. If invalid
syntax is added in the code, the `iib-api` container may shutdown. The Celery worker will
automatically restart if there is a change under the `iib/workers` directory.

To run a built index image from the development registry, you can perform the following:

```bash
podman login --tls-verify=false -u iib -p iibpassword localhost:8443
podman pull --tls-verify=false localhost:8443/iib-build:1
```

If you are using Docker (a modern version is required), you can perform the following:

```bash
sudo docker login -u iib -p iibpassword localhost:8443
sudo docker run localhost:8443/iib-build:1
```

If your development environment requires accessing a private container registry, please read
the section titled Registry Authentication.

You may also run the development environment with
[podman-compose](https://github.com/containers/podman-compose). Use the script from the `devel`
branch as it has various fixes and new features required to run IIB. Set the environment variable
`IIB_COMPOSE_ENGINE` to the path of the `podman-compose` script before running the `make` commands.

## Dependency Management

To manage dependencies, this project uses [pip-tools](https://github.com/jazzband/pip-tools) so that
the production dependencies are pinned and the hashes of the dependencies are verified during
installation.

The unpinned dependencies are recorded in `setup.py`, and to generate the `requirements.txt` file,
run `pip-compile --generate-hashes --output-file=requirements.txt`. This is only necessary when
adding a new package. To upgrade a package, use the `-P` argument of the `pip-compile` command.

To update `requirements-test.txt`, run
`pip-compile --generate-hashes requirements-test.in -o requirements-test.txt`.

When installing the dependencies in a production environment, run
`pip install --require-hashes -r requirements.txt`. Alternatively, you may use
`pip-sync requirements.txt`, which will make sure your virtualenv only has the packages listed in
`requirements.txt`.

To ensure the pinned dependencies are not vulnerable, this project uses
[safety](https://github.com/pyupio/safety), which runs on every pull-request.

## Registry Authentication

IIB does not handle authentication with container registries directly. If authentication is needed,
configure the `~/.docker/config.json.template` file for the user running the IIB worker. This path
can be customized with the `iib_docker_config_template` configuration.

During development, you may choose to add a volume entry of
`- /root/.docker/config.json:/root/.docker/config.json.template:ro,z` on the workers in
`docker-compose.yml` so that your host's root user's Docker configuration with authentication is
used by the workers. This is only needed if you are working with private images.

## Configuring the REST API

To configure the IIB REST API, create a Python file at `/etc/iib/settings.py`. Any variables set in
this configuration file will be applied to the Celery worker when running in production mode
(default).

The custom configuration options for the REST API are listed below:

* `IIB_ADDITIONAL_LOGGERS` - a list of Python loggers that should have the same log level that is
  set for `IIB_LOG_LEVEL`. This defaults to `[]`.
* `IIB_BINARY_IMAGE_CONFIG` - the mapping, `dict(<str>: dict(<str>:<str>))`, of distribution scope
  to another dictionary mapping ocp_version label to a binary image pull specification.
  This is useful in setting up customized binary image for different index image images thus
  reducing complexity for the end user. This defaults to `{}`.
* `IIB_FORCE_OVERWRITE_FROM_INDEX` - a boolean that determines if privileged users should be forced
  to have `overwrite_from_index` set to `True`. This defaults to `False`.
* `IIB_GREENWAVE_CONFIG` - the mapping, `dict(<str>: dict(<str>:<str>))`, of celery task queues to
  another dictionary of [Greenwave](https://docs.pagure.org/greenwave/) query parameters to their
  values. This is useful in setting up customized gating for each queue. This defaults to `{}`. Use
  the task queue name as `None` to configure Greenwave config for the default Celery task queue.
* `IIB_LOG_FORMAT` - the format of the logs. This defaults to
  `%(asctime)s %(name)s %(levelname)s %(module)s.%(funcName)s %(message)s`.
* `IIB_LOG_LEVEL` - the Python log level of the REST API (Flask). This defaults to `INFO`.
* `IIB_MAX_PER_PAGE` - the maximum number of build requests that can be shown on a single page.
  This defaults to `20`.
* `IIB_PRIVILEGED_USERNAMES` - the list of users that can perform privileged actions such
  as overwriting the input index image with the built index image. This defaults to `[]`.
* `IIB_REQUEST_LOGS_DIR` - the directory to load the request specific log files. If `None`, per
  request log files information will not appear in the API response. This defaults to `None`.
* `IIB_REQUEST_LOGS_DAYS_TO_LIVE` - the amount of days after which per request logs are considered
  to be expired and may be removed. This defaults to `3`.
* `IIB_USER_TO_QUEUE` - the mapping, `dict(<str>: <str>)`, of usernames to celery task queues.
  This is useful in isolating the workload from certain users. Some celery tasks must execute
  serially, while others can execute in parallel. Add the prefix `SERIAL:` or `PARALLEL:` to the
  **username** key in this mapping to create queues based on serial vs parallel tasks. The default
  queue is used for tasks from users not found in the mapping. This defaults to `{}`.
* `IIB_WORKER_USERNAMES` - the list of case-sensitve Kerberos principals that are allowed to update
  build requests using the PATCH API endpoint. This defaults to `[]`.
* `LOGIN_DISABLED` - determines if authentication is required. This defaults to `False`
  (i.e. authentication is required).
* `SQLALCHEMY_DATABASE_URI` - the database URI of the database the REST API connects to. See the
  [Flask-SQLAlchemy configuration](https://flask-sqlalchemy.palletsprojects.com/en/2.x/config/#configuration-keys)
  documentation.

The custom configuration options for AMQP 1.0 messaging are listed below:

* `IIB_MESSAGING_BATCH_STATE_DESTINATION` - the AMQP 1.0 destination to send the batch state change
  messages. If this is not set, IIB will not send these types of messages. If this is set,
  `IIB_MESSAGING_URLS` must also be set.
* `IIB_MESSAGING_BUILD_STATE_DESTINATION` - the AMQP 1.0 destination to send the build request state
  change messages. If this is not set, IIB will not send these types of messages. If this is set,
  `IIB_MESSAGING_URLS` must also be set.
* `IIB_MESSAGING_CA` - the path to a file with the certificate authority that signed the certificate
  of the AMQP 1.0 message broker. This defaults to `/etc/pki/tls/certs/ca-bundle.crt`.
* `IIB_MESSAGING_CERT` - the path to the identity certificate used for authentication with the
  AMQP 1.0 message broker. This defaults to `/etc/iib/messaging.crt`.
* `IIB_MESSAGING_DURABLE` - determines if the messages are durable and cannot be lost due to an
  unexpected termination or restart by the AMQP 1.0 broker. If the broker is not capable of
  guaranteeing this, it may not accept the message. In that case, set this configuration option to
  `False`. This defaults to `True`.
* `IIB_MESSAGING_KEY` - the path to the private key of the identity certificate used for
  authentication with the AMQP 1.0 message broker. This defaults to `/etc/iib/messaging.key`.
* `IIB_MESSAGING_TIMEOUT` - the number of seconds before a messaging operation times out.
  Examples of messaging operations include connecting to the broker and sending a message to the
  broker. In this case, if the timeout is set to `30`, then it could take a maximum of 60 seconds
  before the operation times out. This is because it can take up to 30 seconds to connect to the
  broker and also up to 30 seconds for the message to be sent. This defaults to `30`.
* `IIB_MESSAGING_URLS` - a list of AMQP(S) URLs to use when connecting to the AMQP 1.0 broker. This
  must be set if messaging is enabled.

## Configuring the Worker(s)

To configure an IIB Celery worker, create a Python file at `/etc/iib/celery.py`. The location
can be overridden with the `IIB_CELERY_CONFIG` environment variable. This is useful if the worker is
running on the same host as another worker or the REST API.

Any variables set in this configuration file will be applied to the Celery worker when running in
production mode (default).

The custom configuration options for the Celery workers are listed below:

* `broker_url` - the AMQP(S) URL to connect to RabbitMQ. See the
  [broker_url](https://docs.celeryproject.org/en/latest/userguide/configuration.html#std:setting-broker_url)
  configuration documentation.
* `iib_api_timeout` - the timeout in seconds for HTTP requests to the REST API. This defaults to
  `30` seconds.
* `iib_api_url` - the URL to the IIB REST API (e.g. `https://iib.domain.local/api/v1/`).
* `iib_docker_config_template` - the path to the Docker config.json file for IIB to use as a
  template. IIB will symlink this file to `~/.docker/config.json` at the beginning of every request.
  Additionally, it will use this file as a base and set the `overwrite_from_index_token` for the
  registry of the `from_index` container image when applicable. IIB will never directly modify this
  file though. This defaults to `~/.docker/config.json.template`.
*  `iib_dogpile_backend` - the configuration for the dogpile.cache backend. The default value is
   `'dogpile.cache.null'`. In case you want to enable caching, set this to `'dogpile.cache.memcached'`.
*  `iib_dogpile_expiration_time` - the number of seconds after which the cached item is expired.
*   `iib_dogpile_arguments` - additional arguments for the dogpile backend.
* `iib_greenwave_url` - the URL to the Greenwave REST API if gating is desired
  (e.g. `https://greenwave.domain.local/api/v1.0/`). This defaults to `None`.
* `iib_grpc_init_wait_time` - time to wait for the index image service to be initialized. This
  defaults to `3` seconds.
* `iib_grpc_max_port_tries` - maximum ports to try when initializing the index image service.
  This defaults to `100` tries.
* `iib_grpc_start_port` - first port to try when starting the service (subsequent are increments).
  This defaults to `50051`.
* `iib_grpc_max_tries` - maximum number of times to try to start the index image service
  before giving up. This defaults to `5` attempts.
* `iib_index_image_output_registry` - if set, that value will replace the value from `iib_registry`
  in the output `index_image` pull specification. This is useful if you'd like users of IIB to
  pull from a proxy to a registry instead of the registry directly.
* `iib_image_push_template` - the Python string template of the push destination for the resulting
  manifest list. The available variables are `registry` and `request_id`. The default value is
  `{registry}/iib-build:{request_id}`.
* `iib_log_level` - the Python log level for `iib.workers` logger. This defaults to `INFO`.
* `iib_organization_customizations` - this is used to customize aspects of the bundle being
  regenerated. The format is a dictionary where each key is an organization that requires
  customizations. Each value is a list of dictionaries with the ``type`` key set to one of the
  optional values `csv_annotations`, `package_name_suffix`,
  and `registry_replacements`. The order of the dictionaries in the list will determine the order
  of customizations applied to the bundle.

  * The `csv_annotations` customization type is a dictionary where the key `annotations` value is
    a dictionary where each key is the annotation to set on the ClusterServiceVersion files, and
    the value is a Python template string of the value to be set. IIB only substitutes
    `{package_name}` in the template string.
  * The `package_name_suffix` customization type is a dictionary where the key `suffix` value is
    a string of a suffix to add to the package name of the operator.
  * The `registry_replacements` customization type is a dictionary where the key `replacements`
    value is a dictionary where the keys are the old registries to replace and the values
    are the registries to replace the old registries with. This replaces the registry in all
    the ClusterServiceVersion files.
  * The `image_name_from_labels` customization type is a dictionary where the key `template`
    value is a string which specifies a combination of label names in curly braces which will be
    substituted with the actual label values from the bundle image.
  * The `enclose_repo` customization type is a dictionary where the key `enclosure_glue` value
    is a string which specifies the glue to replace ``/`` (forward slashes) in the pull spec
    name and repo. The key `namespace` value is also a string which specifies the new namespace
    for pull specs of the ClusterServiceVersion files.

  Here is an example that ties this all together:

  ```python
  iib_organization_customizations = {
        'company-marketplace': [
            {
                'type': 'csv_annotations',
                'annotations': {
                    'marketplace.company.io/remote-workflow': (
                        'https://marketplace.company.com/en-us/operators/{package_name}/pricing'
                    ),
                    'marketplace.company.io/support-workflow': (
                        'https://marketplace.company.com/en-us/operators/{package_name}/support'
                    ),
                },
            },
            {'type': 'package_name_suffix', 'suffix': '-cmp'},
            {
                'type': 'registry_replacements',
                'replacements': {
                    'registry.access.company.com': 'registry.marketplace.company.com/cm',
                },
            },
            {'type': 'image_name_from_labels', 'template': '{name}-{version}-final'},
            {'type': 'enclose_repo', 'enclosure_glue': '----', 'namespace': "company-pending"},
        ]
    }
  ```

* `iib_request_logs_dir` - the directory to write the request specific log files. If `None`, per
  request log files are not created. This defaults to `None`.
* `iib_request_logs_format` - the format for the log messages of the request specific log files.
  This defaults to `%(asctime)s %(name)s %(levelname)s %(module)s.%(funcName)s %(message)s`.
* `iib_request_logs_level` - the log level for the request specific log files. This defaults to
  `DEBUG`.
* `iib_registry` - the container registry to push images to (e.g. `quay.io`).
* `iib_skopeo_timeout` - the command timeout for skopeo commands run by IIB. This defaults to
  `30s` (30 seconds).
* `iib_total_attempts` - the total number of attempts to make at trying a function relating to the
  container registry before erroring out. This defaults to `5`.

## Regenerating Bundle Images

In addition to building operator index images, IIB can also be used to regenerate operator bundle
images. This is useful for applying modifications to the manifests embedded in the bundle image.
IIB uses the [operator-manifest](https://github.com/containerbuildsystem/operator-manifest) library
to assist in these modifications.

Currently, IIB will not perform any modifications on a ClusterServiceVersion file if
[spec.relatedImages](https://access.redhat.com/documentation/en-us/openshift_container_platform/4.3/html-single/operators/index#olm-enabling-operator-for-restricted-network_osdk-generating-csvs)
is set.

If it's not set, IIB will pin any container image pull specification and set
[spec.relatedImages](https://access.redhat.com/documentation/en-us/openshift_container_platform/4.3/html-single/operators/index#olm-enabling-operator-for-restricted-network_osdk-generating-csvs).
See the different
[pull specifications](https://github.com/containerbuildsystem/operator-manifest#pull-specifications)
to which this process applies to. There are also a variety of customizations that can be made to
the bundle. See the `iib_organization_customizations` configuration option for more details.

Bundle images regenerated by IIB will have the label `com.redhat.iib.pinned` set to `'true'`.
If the bundle image already has this label set to this value, pinning is skipped. Any other
modifications, such as registry replacement, will still be applied.

## Messaging

IIB has support to send messages to an AMQP 1.0 broker. If configured to do so, IIB will send
messages when a build request state changes and when a batch state changes. Please note that if a
message can't be sent due to an infrastructure issue, the build request will continue as it is not
considered a fatal error.

The build request state change message body is the JSON representation of the build request in
the non-verbose format like in the `/builds` API endpoint. The message has the following keys set in
the application properties: `batch`, `id`, `state`, and `user`.

The batch state change message body is a JSON object with the following keys: `annotations`,
`batch`, `requests`, `state`, and `user`. The `requests` value is an array of JSON objects with the
keys `id`, `organization`, and `request_type`. The message has the following keys set in the
application properties: `batch`, `state`, and `user`.

## Gating Bundle Images

In addition to building operator index images, IIB can also gate your bundle images before adding
them to the index image. If a Greenwave configuration is setup for your queue, IIB will query
Greenwave to check if your bundle image builds have passed the tests in the Greenwave policy you
have defined. The IIB request submitted to that queue will succeed only if the policy is satisfied.

## Read the Docs Documentation

### Build the Docs

To build and serve the docs, run the following commands:

```bash
tox -e docs
google-chrome .tox/docs_out/index.html
```

### Expanding the Docs

To document a new Python module, find the `rst` file of the corresponding Python package that
contains the module. Once found, add a section under "Submodules" in alphabetical order such as:

```rst
iib.workers.tasks.build module
------------------------------

.. automodule:: iib.workers.tasks.build
   :ignore-module-all:
   :members:
   :private-members:
   :show-inheritance:
```

Some of the options include:

* `ignore-module-all` - include all members regardless of the definition of `__all__`.
* `members` - automatically document the members in that Python module.
* `private-members` - include private functions and methods.
* `show-inheritance` - show the class inheritance.
