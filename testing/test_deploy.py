#!/usr/bin/env python

# Copyright 2018 The Kubeflow Authors All rights reserved.
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

"""Test deploying Kubeflow.

Requirements:
  This project assumes the py directory in github.com/kubeflow/tf-operator corresponds
  to a top level Python package on the Python path.

  TODO(jlewi): Come up with a better story for how we reuse the py package
  in kubeflow/tf-operator. We should probably turn that into a legit Python pip
  package that is built and released as part of the kubeflow/tf-operator project.
"""

import argparse
import datetime
import logging
import os
import shutil
import tempfile
import uuid

from kubernetes import client as k8s_client
from kubernetes.client import rest
from kubernetes.config import incluster_config

from kubeflow.testing import test_util
from kubeflow.testing import util

def _setup_test(api_client, run_label):
  """Create the namespace for the test.

  Returns:
    test_dir: The local test directory.
  """

  api = k8s_client.CoreV1Api(api_client)
  namespace = k8s_client.V1Namespace()
  namespace.api_version = "v1"
  namespace.kind = "Namespace"
  namespace.metadata = k8s_client.V1ObjectMeta(name=run_label, labels={
    "app": "kubeflow-e2e-test",
    }
  )

  try:
    logging.info("Creating namespace %s", namespace.metadata.name)
    namespace = api.create_namespace(namespace)
    logging.info("Namespace %s created.", namespace.metadata.name)
  except rest.ApiException as e:
    if e.status == 409:
      logging.info("Namespace %s already exists.", namespace.metadata.name)
    else:
      raise

  return namespace

def create_k8s_client(args):
  if args.cluster:
    project = args.project
    cluster_name = args.cluster
    zone = args.zone
    logging.info("Using cluster: %s in project: %s in zone: %s",
                 cluster_name, project, zone)
    # Print out config to help debug issues with accounts and
    # credentials.
    util.run(["gcloud", "config", "list"])
    util.configure_kubectl(project, zone, cluster_name)
    util.load_kube_config()
  else:
    # TODO(jlewi): This is sufficient for API access but it doesn't create
    # a kubeconfig file which ksonnet needs for ks init.
    logging.info("Running inside cluster.")
    incluster_config.load_incluster_config()

  # Create an API client object to talk to the K8s master.
  api_client = k8s_client.ApiClient()

  return api_client

# TODO(jlewi): We should make this a reusable function in kubeflow/testing
# because we will probably want to use it in other places as well.
def setup_kubeflow_ks_app(args, api_client):
  """Create a ksonnet app for Kubeflow"""
  if not os.path.exists(args.test_dir):
    os.makedirs(args.test_dir)

  logging.info("Using test directory: %s", args.test_dir)

  namespace_name = args.namespace

  namespace = _setup_test(api_client, namespace_name)
  logging.info("Using namespace: %s", namespace)
  if args.github_token:
    logging.info("Setting GITHUB_TOKEN to %s.", args.github_token)
    # Set a GITHUB_TOKEN so that we don't rate limited by GitHub;
    # see: https://github.com/ksonnet/ksonnet/issues/233
    os.environ["GITHUB_TOKEN"] = args.github_token

  if not os.getenv("GITHUB_TOKEN"):
    logging.warn("GITHUB_TOKEN not set; you will probably hit Github API "
                 "limits.")
  # Initialize a ksonnet app.
  app_name = "kubeflow-test"
  util.run(["ks", "init", app_name,], cwd=args.test_dir)

  app_dir = os.path.join(args.test_dir, app_name)

  kubeflow_registry = "github.com/kubeflow/kubeflow/tree/master/kubeflow"
  util.run(["ks", "registry", "add", "kubeflow", kubeflow_registry], cwd=app_dir)

  # Install required packages
  packages = ["kubeflow/core", "kubeflow/tf-serving", "kubeflow/tf-job"]

  for p in packages:
    util.run(["ks", "pkg", "install", p], cwd=app_dir)

  # Delete the vendor directory and replace with a symlink to the src
  # so that we use the code at the desired commit.
  target_dir = os.path.join(app_dir, "vendor", "kubeflow")

  logging.info("Deleting %s", target_dir)
  shutil.rmtree(target_dir)

  REPO_ORG = "kubeflow"
  REPO_NAME = "kubeflow"
  REGISTRY_PATH = "kubeflow"
  source = os.path.join(args.test_dir, "src", REPO_ORG, REPO_NAME,
                        REGISTRY_PATH)
  logging.info("Creating link %s -> %s", target_dir, source)
  os.symlink(source, target_dir)

  return app_dir

def setup(args):
  """Test deploying Kubeflow."""
  api_client = create_k8s_client(args)
  app_dir = setup_kubeflow_ks_app(args, api_client)

  namespace = args.namespace
  # TODO(jlewi): We don't need to generate a core component if we are
  # just deploying TFServing. Might be better to refactor this code.
  # Deploy Kubeflow
  util.run(["ks", "generate", "core", "kubeflow-core", "--name=kubeflow-core",
            "--namespace=" + namespace], cwd=app_dir)

  # TODO(jlewi): For reasons I don't understand even though we ran
  # configure_kubectl above, if we don't rerun it we get rbac errors
  # when we do ks apply; I think because we aren't using the proper service
  # account. This might have something to do with the way ksonnet gets
  # its credentials; maybe we need to configure credentials after calling
  # ks init?
  if args.cluster:
    util.configure_kubectl(args.project, args.zone, args.cluster)

  apply_command = ["ks", "apply", "default", "-c", "kubeflow-core",]

  util.run(apply_command, cwd=app_dir)

  # Verify that the TfJob operator is actually deployed.
  tf_job_deployment_name = "tf-job-operator"
  logging.info("Verifying TfJob controller started.")
  util.wait_for_deployment(api_client, namespace,
                           tf_job_deployment_name)

  # Verify that JupyterHub is actually deployed.
  jupyter_name = "tf-hub"
  logging.info("Verifying TfHub started.")
  util.wait_for_statefulset(api_client, namespace, jupyter_name)

def deploy_model(args):
  """Deploy a TF model using the TF serving component."""
  api_client = create_k8s_client(args)
  app_dir = setup_kubeflow_ks_app(args, api_client)

  component = "modelServer"
  logging.info("Deploying tf-serving.")
  generate_command = [
      "ks", "generate", "tf-serving", component,
      "--name=inception",]

  util.run(generate_command, cwd=app_dir)

  params = {}
  for pair in args.params.split(","):
    k, v = pair.split("=", 1)
    params[k] = v

  if "namespace" not in params:
    raise ValueError("namespace must be supplied via --params.")
  namespace = params["namespace"]

  # Set env to none so random env will be created.
  ks_deploy(app_dir, component, params, env=None, account=None)

  core_api = k8s_client.CoreV1Api(api_client)
  deploy = core_api.read_namespaced_service(
    "inception", args.namespace)
  cluster_ip = deploy.spec.cluster_ip

  if not cluster_ip:
    raise ValueError("inception service wasn't assigned a cluster ip.")
  util.wait_for_deployment(api_client, namespace, "inception")
  logging.info("Verified TF serving started.")

def teardown(args):
  # Delete the namespace
  logging.info("Deleting namespace %s", args.namespace)
  api_client = create_k8s_client(args)
  core_api = k8s_client.CoreV1Api(api_client)
  core_api.delete_namespace(args.namespace, {})

def determine_test_name(args):
  return args.func.__name__

# TODO(jlewi): We should probably make this a generic function in
# kubeflow.testing.`
def wrap_test(args):
  """Run the tests given by args.func and output artifacts as necessary.
  """
  test_name = determine_test_name(args)
  test_case = test_util.TestCase()
  test_case.class_name = "KubeFlow"
  test_case.name = "deploy-kubeflow-" + test_name
  try:
    def run():
      args.func(args)

    test_util.wrap_test(run, test_case)
  finally:

    junit_path = os.path.join(
      args.artifacts_dir, "junit_kubeflow-deploy-{0}.xml".format(test_name))
    logging.info("Writing test results to %s", junit_path)
    test_util.create_junit_xml_file([test_case], junit_path)


# TODO(jlewi): We should probably make this a reusable function since a
# lot of test code code use it.
def ks_deploy(app_dir, component, params, env=None, account=None):
  """Deploy the specified ksonnet component.
  Args:
    app_dir: The ksonnet directory
    component: Name of the component to deployed
    params: A dictionary of parameters to set; can be empty but should not be
      None.
    env: (Optional) The environment to use, if none is specified a new one
      is created.
    account: (Optional) The account to use.
  Raises:
    ValueError: If input arguments aren't valid.
  """
  if not component:
    raise ValueError("component can't be None.")

  # TODO(jlewi): It might be better if the test creates the app and uses
  # the latest stable release of the ksonnet configs. That however will cause
  # problems when we make changes to the TFJob operator that require changes
  # to the ksonnet configs. One advantage of checking in the app is that
  # we can modify the files in vendor if needed so that changes to the code
  # and config can be submitted in the same pr.
  now = datetime.datetime.now()
  if not env:
    env = "e2e-" + now.strftime("%m%d-%H%M-") + uuid.uuid4().hex[0:4]

  logging.info("Using app directory: %s", app_dir)

  util.run(["ks", "env", "add", env], cwd=app_dir)

  for k, v in params.iteritems():
    util.run(
      ["ks", "param", "set", "--env=" + env, component, k, v], cwd=app_dir)

  apply_command = ["ks", "apply", env, "-c", component]
  if account:
    apply_command.append("--as=" + account)
  util.run(apply_command, cwd=app_dir)

def main():  # pylint: disable=too-many-locals
  logging.getLogger().setLevel(logging.INFO) # pylint: disable=too-many-locals
  # create the top-level parser
  parser = argparse.ArgumentParser(
    description="Test Kubeflow E2E.")

  parser.add_argument(
    "--test_dir",
    default="",
    type=str,
    help="Directory to use for all the test files. If not set a temporary "
         "directory is created.")

  parser.add_argument(
    "--artifacts_dir",
    default="",
    type=str,
    help="Directory to use for artifacts that should be preserved after "
         "the test runs. Defaults to test_dir if not set.")

  parser.add_argument(
    "--project",
    default=None,
    type=str,
    help="The project to use.")

  parser.add_argument(
    "--cluster",
    default=None,
    type=str,
    help=("The name of the cluster. If not set assumes the "
          "script is running in a cluster and uses that cluster."))

  parser.add_argument(
    "--namespace",
    required=True,
    type=str,
    help=("The namespace to use."))

  parser.add_argument(
    "--zone",
    default="us-east1-d",
    type=str,
    help="The zone for the cluster.")

  parser.add_argument(
    "--github_token",
    default=None,
    type=str,
    help=("The GitHub API token to use. This is needed since ksonnet uses the "
          "GitHub API and without it we get rate limited. For more info see: "
          "https://github.com/ksonnet/ksonnet/blob/master/docs"
          "/troubleshooting.md. Can also be set using environment variable "
          "GITHUB_TOKEN."))

  subparsers = parser.add_subparsers()

  parser_setup = subparsers.add_parser(
    "setup",
    help="setup the test infrastructure.")

  parser_setup.set_defaults(func=setup)

  parser_teardown = subparsers.add_parser(
    "teardown",
    help="teardown the test infrastructure.")

  parser_teardown.set_defaults(func=teardown)


  parser_tf_serving = subparsers.add_parser(
    "deploy_model",
    help="Deploy a TF serving model.")

  parser_tf_serving.set_defaults(func=deploy_model)

  parser_tf_serving.add_argument(
    "--params",
    default="",
    type=str,
    help=("Comma separated list of parameters to set on the model."))

  args = parser.parse_args()

  if not args.test_dir:
    logging.info("--test_dir not set; using a temporary directory.")

    now = datetime.datetime.now()
    label = "test_deploy-" + now.strftime("%m%d-%H%M-") + uuid.uuid4().hex[0:4]

    # Create a temporary directory for this test run
    args.test_dir = os.path.join(tempfile.gettempdir(), label)

  if not args.artifacts_dir:
    args.artifacts_dir = args.test_dir

  test_log = os.path.join(args.artifacts_dir, "logs",
                          "test_deploy." + args.func.__name__ + ".log.txt")
  if not os.path.exists(os.path.dirname(test_log)):
    os.makedirs(os.path.dirname(test_log))

  # TODO(jlewi): We should make this a util routine in kubeflow.testing.util
  # Setup a logging file handler. This way we can upload the log outputs
  # to gubernator.
  root_logger = logging.getLogger()

  file_handler = logging.FileHandler(test_log)
  root_logger.addHandler(file_handler)
  # We need to explicitly set the formatter because it will not pick up
  # the BasicConfig.
  formatter = logging.Formatter(fmt=("%(levelname)s|%(asctime)s"
                                     "|%(pathname)s|%(lineno)d| %(message)s"),
                                datefmt="%Y-%m-%dT%H:%M:%S")
  file_handler.setFormatter(formatter)
  logging.info("Logging to %s", test_log)

  util.maybe_activate_service_account()

  wrap_test(args)

if __name__ == "__main__":
  logging.basicConfig(level=logging.INFO,
                      format=('%(levelname)s|%(asctime)s'
                              '|%(pathname)s|%(lineno)d| %(message)s'),
                      datefmt='%Y-%m-%dT%H:%M:%S',
                      )
  logging.getLogger().setLevel(logging.INFO)
  main()
