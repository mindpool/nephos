#   Copyright [2018] [Alejandro Vicente Grabovetsky via AID:Tech]
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at#
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

from os import path
from time import sleep

from kubernetes.client.rest import ApiException
from nephos.fabric.settings import get_namespace, get_version
from nephos.fabric.utils import get_helm_pod
from nephos.helpers.helm import (
    HelmPreserve,
    helm_check,
    helm_extra_vars,
    helm_install,
    helm_upgrade,
)
from nephos.helpers.k8s import ingress_read, secret_read
from nephos.helpers.misc import execute_until_success

CURRENT_DIR = path.abspath(path.split(__file__)[0])


# Core sub-functions
def ca_chart(opts, release, upgrade=False, verbose=False):
    """Deploy CA Helm chart to K8S.

    Args:
        opts (dict): Nephos options dict.
        release (str): Name of the Helm Chart release.
        upgrade (bool): Do we upgrade the deployment? False by default.
        verbose (bool): Verbosity. False by default.
    """
    values_dir = opts["core"]["dir_values"]
    repository = opts["core"]["chart_repo"]
    ca_namespace = get_namespace(opts, ca=release)
    # PostgreSQL (Upgrades here are dangerous, deactivated by default)
    # Upgrading database is risky, so we disallow it by default
    if not upgrade:
        version = get_version(opts, "postgresql")
        config_yaml = "{dir}/postgres-ca/{name}-pg.yaml".format(
            dir=values_dir, name=release
        )
        extra_vars = helm_extra_vars(version=version, config_yaml=config_yaml)
        helm_install(
            "stable",
            "postgresql",
            "{}-pg".format(release),
            ca_namespace,
            extra_vars=extra_vars,
            verbose=verbose,
        )
        helm_check("postgresql", "{}-pg".format(release), ca_namespace)
    psql_secret = secret_read(
        "{}-pg-postgresql".format(release), ca_namespace, verbose=verbose
    )
    # Different key depending of PostgreSQL version
    psql_password = (
        psql_secret.get("postgres-password") or psql_secret["postgresql-password"]
    )
    # Fabric CA
    version = get_version(opts, "hlf-ca")
    env_vars = [("externalDatabase.password", psql_password)]
    config_yaml = "{dir}/hlf-ca/{name}.yaml".format(dir=values_dir, name=release)
    if not upgrade:
        extra_vars = helm_extra_vars(
            version=version, config_yaml=config_yaml, env_vars=env_vars
        )
        helm_install(
            repository,
            "hlf-ca",
            release,
            ca_namespace,
            extra_vars=extra_vars,
            verbose=verbose,
        )
    else:
        preserve = (
            HelmPreserve(
                ca_namespace,
                "{}-hlf-ca--ca".format(release),
                "CA_ADMIN",
                "adminUsername",
            ),
            HelmPreserve(
                ca_namespace,
                "{}-hlf-ca--ca".format(release),
                "CA_PASSWORD",
                "adminPassword",
            ),
        )
        extra_vars = helm_extra_vars(
            version=version,
            config_yaml=config_yaml,
            env_vars=env_vars,
            preserve=preserve,
        )
        helm_upgrade(
            repository, "hlf-ca", release, extra_vars=extra_vars, verbose=verbose
        )
    helm_check("hlf-ca", release, ca_namespace)


def ca_enroll(pod_exec):
    """Enroll CA.

    Enroll the Certificate Authority (CA) identity within the running CA pod.
    This is a necessary step for the CA to function.

    Args:
        pod_exec: A pod executor instance bound to the CA.
    """
    alive = False
    while not alive:
        res = pod_exec.logs()
        if "Listening on" in res:
            alive = True
        else:
            sleep(15)
    # Enroll CA Admin if necessary
    ca_cert, _ = pod_exec.execute(
        "cat /var/hyperledger/fabric-ca/msp/signcerts/cert.pem"
    )
    if not ca_cert:
        enrolled_id = False
        while not enrolled_id:
            res, err = pod_exec.execute(
                "bash -c 'fabric-ca-client enroll -d -u http://$CA_ADMIN:$CA_PASSWORD@$SERVICE_DNS:7054'"
            )
            if not err:
                enrolled_id = True
            else:
                sleep(15)


def check_ca(ingress_host, cacert=None, verbose=False):
    """Check that the CA Ingress is responsive.

    Args:
        ingress_host (str): Ingress host for the CA.
        cacert (str): Path of the CA cert.
        verbose (bool): Verbosity. False by default.
    """
    # Check that CA ingress is operational
    command = "curl https://{ingress}/cainfo".format(ingress=ingress_host)
    if cacert:
        command += " --cacert {}".format(cacert)
    execute_until_success(command, verbose=verbose)


# Runner
def setup_ca(opts, upgrade=False, verbose=False):
    """Setup CA.

    Setup involves enrolling the CA admin, checking the Ingress
    is responsive.

    Args:
        opts (dict): Nephos options dict.
        upgrade (bool): Do we upgrade the deployment? False by default.
        verbose (bool): Verbosity. False by default.
    """
    for ca_name, ca_values in opts["cas"].items():
        ca_namespace = get_namespace(opts, ca=ca_name)
        # Install Charts
        ca_chart(opts=opts, release=ca_name, upgrade=upgrade, verbose=verbose)

        # Obtain CA pod and Enroll
        pod_exec = get_helm_pod(
            namespace=ca_namespace, release=ca_name, app="hlf-ca", verbose=verbose
        )
        ca_enroll(pod_exec)

        # Get CA Ingress and check it is running
        try:
            # Get ingress of CA
            ingress_urls = ingress_read(
                ca_name + "-hlf-ca", namespace=ca_namespace, verbose=verbose
            )
        except ApiException:
            print("No ingress found for CA")
            continue

        # Check the CA is running
        check_ca(
            ingress_host=ingress_urls[0],
            cacert=ca_values.get("tls_cert"),
            verbose=verbose,
        )
