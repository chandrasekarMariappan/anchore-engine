import copy
import logging
import os
import time
from os.path import dirname as _dir

import pytest

from tests.functional import get_logger
from tests.functional.services.api.images import get_image_id
from tests.functional.services.utils.docker_utils import create_docker_client
from tests.functional.services.utils.http_utils import (
    RequestFailedError,
    get_api_conf,
    http_del,
    http_get,
    http_post,
    http_put,
)

FT_ACCOUNT = "functional_test"
DELETE_ACCOUNT_TIMEOUT_SEC = 60 * 5

_logger = get_logger(__name__)


def pytest_sessionstart(session):
    BASE_FORMAT = "[%(name)s][%(levelname)-6s] %(message)s"
    FILE_FORMAT = "[%(asctime)s]" + BASE_FORMAT

    root_logger = logging.getLogger("conftest")
    dir_path = os.path.dirname(os.path.realpath(__file__))
    top_level = _dir(_dir(dir_path))
    log_file = os.path.join(top_level, "pytest-functional-tests.log")

    root_logger.setLevel(logging.DEBUG)

    # File Logger
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(FILE_FORMAT, "%Y-%m-%d %H:%M:%S"))

    root_logger.addHandler(fh)


def get_ft_user():
    return {
        "username": os.environ.get("ANCHORE_FUNCTIONAL_TEST__ACCOUNT_USER", "ft_user"),
        "password": os.environ.get(
            "ANCHORE_FUNCTIONAL_TEST_ACCOUNT_PASSWORD", "f00b4r"
        ),
    }


def get_ft_user_api_conf():
    """
    Get a non-root admin user for the functional_test account, that can be used to configure API calls.
    Note: In order to be compatible with http_utils methods, this needs to return an object like
    DEFAULT_API_CONF, ex:
        {
            'ANCHORE_API_USER': '<username>',
            'ANCHORE_API_PASS': '<password>',
            'ANCHORE_BASE_URL': '<base_url>',
            'ANCHORE_API_ACCOUNT': '<account_name>'
        }

    This function translates what is generated by the session-scoped fixture below:
    create_functional_test_account_with_teardown
    """

    # Don't need to do deep copy because api_conf should be single level
    api_conf = copy.copy(get_api_conf())
    ft_user = get_ft_user()

    # override fields (except for base_url)
    api_conf["ANCHORE_API_USER"] = ft_user["username"]
    api_conf["ANCHORE_API_PASS"] = ft_user["password"]
    api_conf["ANCHORE_API_ACCOUNT"] = FT_ACCOUNT

    return api_conf


def does_ft_account_exist():
    ft_account_resp = http_get(["accounts", FT_ACCOUNT])
    return ft_account_resp.code != 404


@pytest.fixture(scope="session", autouse=True)
def create_functional_test_account_with_teardown(request):
    _logger = logging.getLogger("conftest")
    """
    This fixture implicitly tests get_by_account_name, create, update state, and delete operations, but essentially,
    creates a functional_test account with a user ('ft_user' unless overridden by environment variables), and then
    deletes this account (blocking until deletion is complete) at the end of the test session
    """

    def disable_and_delete_functional_test_account():
        """
        This method wil dynamically, and in a blocking fashion, handle account deletion, which requires that the
        functional_test account be disabled before deletion. If the functional_test account is currently enabled, it
        will disable and then delete the account, waiting for the deletion to complete. If the functional_test account
        is already disabled, it will delete the account,  and wait for the deletion to complete. If the functional_test
        account is currently awaiting deletion, it will wait for the deletion to complete. If the functional_test
        account is not found, it will exit.
        """

        def await_account_deletion():
            """
            This method is helpful for awaiting account deletion of the functional_test account, with a timeout governed
            by DELETE_ACCOUNT_TIMEOUT_SEC. It awaits in 5 second intervals.
            """
            start_time_sec = time.time()
            result = 200
            while result != 404:
                time.sleep(5)
                ft_get_account_resp = http_get(["accounts", FT_ACCOUNT])
                _logger.info(
                    "Waiting for functional_test account to fully delete. Time Elapsed={}sec".format(
                        int(time.time() - start_time_sec)
                    )
                )
                if not (
                    ft_get_account_resp.code == 200 or ft_get_account_resp.code == 404
                ):
                    _logger.error(ft_get_account_resp)
                    raise RequestFailedError(
                        ft_get_account_resp.url,
                        ft_get_account_resp.code,
                        ft_get_account_resp.body,
                    )
                if time.time() - start_time_sec >= DELETE_ACCOUNT_TIMEOUT_SEC:
                    raise TimeoutError(
                        "Timed out waiting for functional_test account to delete"
                    )

                result = ft_get_account_resp.code

        ft_account_resp = http_get(["accounts", FT_ACCOUNT])

        if ft_account_resp.code == 404:
            _logger.info("functional_test account not found")
            return

        state = ft_account_resp.body.get("state")
        if state == "enabled":
            _logger.info("functional_test account found, and enabled. Disabling")
            disable_account_resp = http_put(
                ["accounts", FT_ACCOUNT, "state"], {"state": "disabled"}
            )
            if disable_account_resp.code != 200:
                raise RequestFailedError(
                    disable_account_resp.url,
                    disable_account_resp.code,
                    disable_account_resp.body,
                )
        elif state == "deleting":
            _logger.info(
                "functional_test account found, but is currently being deleted"
            )
            await_account_deletion()
            return

        _logger.info("Deleting functional_test account")
        delete_resp = http_del(["accounts", FT_ACCOUNT])
        if not (delete_resp.code == 200 or delete_resp.code == 404):
            raise RequestFailedError(
                delete_resp.url, delete_resp.code, delete_resp.body
            )
        await_account_deletion()

    # Delete the account if it exists already for some reason (sanity check)
    disable_and_delete_functional_test_account()
    _logger.info("Creating functional_test account")
    create_resp = http_post(
        ["accounts"], {"name": FT_ACCOUNT, "email": "admin@anchore.com"}
    )
    if create_resp.code != 200:
        raise RequestFailedError(create_resp.url, create_resp.code, create_resp.body)

    ft_user = get_ft_user()
    _logger.info("Creating functional_test user: {}".format(ft_user["username"]))
    create_user_resp = http_post(["accounts", FT_ACCOUNT, "users"], ft_user)
    if create_user_resp.code != 200:
        raise RequestFailedError(
            create_user_resp.url, create_user_resp.code, create_user_resp.body
        )

    request.addfinalizer(disable_and_delete_functional_test_account)
    return ft_user


@pytest.fixture(scope="session")
def docker_client():
    return create_docker_client()


USER_API_CONFS = [
    pytest.param(get_api_conf, id="admin_account_root_user"),
    pytest.param(get_ft_user_api_conf, id="functional_test_account_fullcontrol_user"),
]


@pytest.fixture(scope="session", params=USER_API_CONFS)
def add_alpine_latest_image(request):
    """
    Note: the test_subscriptions depends on this bit...because a subscription won't exist if there is no image added.
    For now, leave this as session scoped (we can make the subscription test create it's own images later)
    TODO: decouple test_subscriptions from this
    """

    resp = http_post(["images"], {"tag": "alpine:latest"}, config=request.param)
    if resp.code != 200:
        raise RequestFailedError(resp.url, resp.code, resp.body)
    image_id = get_image_id(resp)

    def remove_image_by_id():
        remove_resp = http_del(
            ["images", "by_id", image_id], query={"force": True}, config=request.param
        )
        if remove_resp.code != 200:
            if not does_ft_account_exist():
                # Because this is a session fixture, can't guarantee the order it runs against the account cleanup
                # Therefore, I've observed this finalizer running after the account is deleted. It's not the end of
                # the world, shouldn't be a failed test. If I make this fixture autouse=True, it has been generating an
                # extra matrix of tests which is worse than just letting the finalizer skip
                _logger.info(
                    "{} account does not exist, ignoring for teardown".format(
                        FT_ACCOUNT
                    )
                )
                return
            raise RequestFailedError(
                remove_resp.url, remove_resp.code, remove_resp.body
            )

    request.addfinalizer(remove_image_by_id)
    return resp, request.param


@pytest.fixture(scope="session", params=USER_API_CONFS)
def make_image_analysis_request(request):
    """
    Returns a function that can be used to add an image with given tag for analysis
    """

    def _add_image_for_analysis(tag):
        return http_post(["images"], {"tag": tag}, config=request.param)

    return _add_image_for_analysis
