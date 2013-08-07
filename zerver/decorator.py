from __future__ import absolute_import

from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.http import QueryDict
from django.http.multipartparser import MultiPartParser
from zerver.models import UserProfile, get_client, get_user_profile_by_email
from zerver.lib.response import json_error, json_unauthorized
from django.utils.timezone import now
from django.conf import settings
import ujson
from StringIO import StringIO
from zerver.lib.queue import queue_json_publish
from zerver.lib.timestamp import datetime_to_timestamp
from zerver.lib.utils import statsd
from zerver.exceptions import RateLimited
from zerver.lib.rate_limiter import incr_ratelimit, is_ratelimited, \
     api_calls_left
from functools import wraps
import base64
import logging
import cProfile

class _RespondAsynchronously(object):
    pass

# Return RespondAsynchronously from an @asynchronous view if the
# response will be provided later by calling handler.zulip_finish(),
# or has already been provided this way. We use this for longpolling
# mode.
RespondAsynchronously = _RespondAsynchronously()

def asynchronous(method):
    @wraps(method)
    def wrapper(request, *args, **kwargs):
        return method(request, handler=request._tornado_handler, *args, **kwargs)
    if getattr(method, 'csrf_exempt', False):
        wrapper.csrf_exempt = True
    return wrapper

def update_user_activity(request, user_profile):
    # update_active_status also pushes to rabbitmq, and it seems
    # redundant to log that here as well.
    if request.META["PATH_INFO"] == '/json/update_active_status':
        return
    event={'type': 'user_activity',
           'query': request.META["PATH_INFO"],
           'user_profile_id': user_profile.id,
           'time': datetime_to_timestamp(now()),
           'client': request.client.name}
    # TODO: It's possible that this should call process_user_activity
    # from zerver.lib.actions for maximal consistency.
    queue_json_publish("user_activity", event, lambda event: None)

# I like the all-lowercase name better
require_post = require_POST

default_clients = {}

def process_client(request, user_profile, default):
    if 'client' in request.REQUEST:
        request.client = get_client(request.REQUEST['client'])
    else:
        if default not in default_clients:
            default_clients[default] = get_client(default)
        request.client = default_clients[default]

    update_user_activity(request, user_profile)

def validate_api_key(email, api_key):
    try:
        user_profile = get_user_profile_by_email(email)
    except UserProfile.DoesNotExist:
        raise JsonableError("Invalid user: %s" % (email,))
    if api_key != user_profile.api_key:
        raise JsonableError("Invalid API key for user '%s'" % (email,))
    if not user_profile.is_active:
        raise JsonableError("User account is not active")
    return user_profile

# authenticated_api_view will add the authenticated user's user_profile to
# the view function's arguments list, since we have to look it up
# anyway.
def authenticated_api_view(view_func):
    @csrf_exempt
    @require_post
    @has_request_variables
    @wraps(view_func)
    def _wrapped_view_func(request, email=REQ, api_key=REQ('api-key'),
                           *args, **kwargs):
        user_profile = validate_api_key(email, api_key)
        request.user = user_profile
        request._email = user_profile.email
        process_client(request, user_profile, "API")
        # Apply rate limiting
        limited_func = rate_limit()(view_func)
        return limited_func(request, user_profile, *args, **kwargs)
    return _wrapped_view_func

def authenticated_rest_api_view(view_func):
    @wraps(view_func)
    def _wrapped_view_func(request, *args, **kwargs):
        # First try block attempts to get the credentials we need to do authentication
        try:
            # Grab the base64-encoded authentication string, decode it, and split it into
            # the email and API key
            auth_type, encoded_value = request.META['HTTP_AUTHORIZATION'].split()
            # case insensitive per RFC 1945
            if auth_type.lower() != "basic":
                return json_error("Only Basic authentication is supported.")
            email, api_key = base64.b64decode(encoded_value).split(":")
        except ValueError:
            return json_error("Invalid authorization header for basic auth")
        except KeyError:
            return json_unauthorized("Missing authorization header for basic auth")

        # Now we try to do authentication or die
        try:
            user_profile = validate_api_key(email, api_key)
        except JsonableError, e:
            return json_unauthorized(e.error)
        request.user = user_profile
        request._email = user_profile.email
        process_client(request, user_profile, "API")
        # Apply rate limiting
        limited_func = rate_limit()(view_func)
        return limited_func(request, user_profile, *args, **kwargs)
    return _wrapped_view_func

def process_as_post(view_func):
    @wraps(view_func)
    def _wrapped_view_func(request, *args, **kwargs):
        # Adapted from django/http/__init__.py.
        # So by default Django doesn't populate request.POST for anything besides
        # POST requests. We want this dict populated for PATCH/PUT, so we have to
        # do it ourselves.
        #
        # This will not be required in the future, a bug will be filed against
        # Django upstream.

        if not request.POST:
            # Only take action if POST is empty.
            if request.META.get('CONTENT_TYPE', '').startswith('multipart'):
                # Note that request._files is just the private attribute that backs the
                # FILES property, so we are essentially setting request.FILES here.  (In
                # Django 1.5 FILES was still a read-only property.)
                request.POST, request._files = MultiPartParser(request.META, StringIO(request.body),
                        request.upload_handlers, request.encoding).parse()
            else:
                request.POST = QueryDict(request.body, encoding=request.encoding)

        return view_func(request, *args, **kwargs)

    return _wrapped_view_func

def authenticate_log_and_execute_json(request, view_func, *args, **kwargs):
    if not request.user.is_authenticated():
        return json_error("Not logged in", status=401)
    user_profile = request.user
    process_client(request, user_profile, "website")
    request._email = user_profile.email
    update_user_activity(request, user_profile)
    return view_func(request, user_profile, *args, **kwargs)

# Checks if the request is a POST request and that the user is logged
# in.  If not, return an error (the @login_required behavior of
# redirecting to a login page doesn't make sense for json views)
def authenticated_json_post_view(view_func):
    @require_post
    @has_request_variables
    @wraps(view_func)
    def _wrapped_view_func(request,
                           *args, **kwargs):
        return authenticate_log_and_execute_json(request, view_func, *args, **kwargs)
    return _wrapped_view_func

def authenticated_json_view(view_func):
    @wraps(view_func)
    def _wrapped_view_func(request,
                           *args, **kwargs):
        return authenticate_log_and_execute_json(request, view_func, *args, **kwargs)
    return _wrapped_view_func

# These views are used by the main Django server to notify the Tornado server
# of events.  We protect them from the outside world by checking a shared
# secret, and also the originating IP (for now).
def authenticate_notify(request):
    return (request.META['REMOTE_ADDR'] in ('127.0.0.1', '::1')
            and request.POST.get('secret') == settings.SHARED_SECRET)

def internal_notify_view(view_func):
    @csrf_exempt
    @require_post
    @wraps(view_func)
    def _wrapped_view_func(request, *args, **kwargs):
        if not authenticate_notify(request):
            return json_error('Access denied', status=403)
        if not hasattr(request, '_tornado_handler'):
            # We got called through the non-Tornado server somehow.
            # This is not a security check; it's an internal assertion
            # to help us find bugs.
            raise RuntimeError, 'notify view called with no Tornado handler'
        request._email = "internal"
        return view_func(request, *args, **kwargs)
    return _wrapped_view_func

class JsonableError(Exception):
    def __init__(self, error):
        self.error = error

    def __str__(self):
        return self.to_json_error_msg()

    def to_json_error_msg(self):
        return self.error

class RequestVariableMissingError(JsonableError):
    def __init__(self, var_name):
        self.var_name = var_name

    def to_json_error_msg(self):
        return "Missing '%s' argument" % (self.var_name,)

class RequestVariableConversionError(JsonableError):
    def __init__(self, var_name, bad_value):
        self.var_name = var_name
        self.bad_value = bad_value

    def to_json_error_msg(self):
        return "Bad value for '%s': %s" % (self.var_name, self.bad_value)

# Used in conjunction with @has_request_variables, below
class REQ(object):
    # NotSpecified is a sentinel value for determining whether a
    # default value was specified for a request variable.  We can't
    # use None because that could be a valid, user-specified default
    class _NotSpecified(object):
        pass
    NotSpecified = _NotSpecified()

    def __init__(self, whence=None, converter=None, default=NotSpecified):
        """
        whence: the name of the request variable that should be used
        for this parameter.  Defaults to a request variable of the
        same name as the parameter.

        converter: a function that takes a string and returns a new
        value.  If specified, this will be called on the request
        variable value before passing to the function

        default: a value to be used for the argument if the parameter
        is missing in the request
        """

        self.post_var_name = whence
        self.func_var_name = None
        self.converter = converter
        self.default = default

# Extracts variables from the request object and passes them as
# named function arguments.  The request object must be the first
# argument to the function.
#
# To use, assign a function parameter a default value that is an
# instance of the REQ class.  That paramter will then be automatically
# populated from the HTTP request.  The request object must be the
# first argument to the decorated function.
#
# This should generally be the innermost (syntactically bottommost)
# decorator applied to a view, since other decorators won't preserve
# the default parameter values used by has_request_variables.
#
# Note that this can't be used in helper functions which are not
# expected to call json_error or json_success, as it uses json_error
# internally when it encounters an error
def has_request_variables(view_func):
    num_params = view_func.func_code.co_argcount
    if view_func.func_defaults is None:
        num_default_params = 0
    else:
        num_default_params = len(view_func.func_defaults)
    default_param_names = view_func.func_code.co_varnames[num_params - num_default_params:]
    default_param_values = view_func.func_defaults
    if default_param_values is None:
        default_param_values = []

    post_params = []

    for (name, value) in zip(default_param_names, default_param_values):
        if isinstance(value, REQ):
            value.func_var_name = name
            if value.post_var_name is None:
                value.post_var_name = name
            post_params.append(value)
        elif value == REQ:
            # If the function definition does not actually instantiate
            # a REQ object but instead uses the REQ class itself as a
            # value, we instantiate it as a convenience
            post_var = value(name)
            post_var.func_var_name = name
            post_params.append(post_var)

    @wraps(view_func)
    def _wrapped_view_func(request, *args, **kwargs):
        for param in post_params:
            if param.func_var_name in kwargs:
                continue

            default_assigned = False
            try:
                val = request.REQUEST[param.post_var_name]
            except KeyError:
                if param.default is REQ.NotSpecified:
                    raise RequestVariableMissingError(param.post_var_name)
                val = param.default
                default_assigned = True

            if param.converter is not None and not default_assigned:
                try:
                    val = param.converter(val)
                except:
                    raise RequestVariableConversionError(param.post_var_name, val)
            kwargs[param.func_var_name] = val

        return view_func(request, *args, **kwargs)

    return _wrapped_view_func

# Converter functions for use with has_request_variables
def to_non_negative_int(x):
    x = int(x)
    if x < 0:
        raise ValueError("argument is negative")
    return x

def json_to_foo(json, type):
    data = ujson.loads(json)
    if not isinstance(data, type):
        raise ValueError("argument is not a %s" % (type().__class__.__name__))
    return data

def json_to_dict(json):
    return json_to_foo(json, dict)

def json_to_list(json):
    return json_to_foo(json, list)

def json_to_bool(json):
    return json_to_foo(json, bool)

def statsd_increment(counter, val=1):
    """Increments a statsd counter on completion of the
    decorated function.

    Pass the name of the counter to this decorator-returning function."""
    def wrapper(func):
        @wraps(func)
        def wrapped_func(*args, **kwargs):
            ret = func(*args, **kwargs)
            statsd.incr(counter, val)
            return ret
        return wrapped_func
    return wrapper

def rate_limit_user(request, user, domain):
    """Returns whether or not a user was rate limited. Will raise a RateLimited exception
    if the user has been rate limited, otherwise returns and modifies request to contain
    the rate limit information"""

    ratelimited, time = is_ratelimited(user, domain)
    request._ratelimit_applied_limits = True
    request._ratelimit_secs_to_freedom = time
    request._ratelimit_over_limit = ratelimited
    # Abort this request if the user is over her rate limits
    if ratelimited:
        statsd.incr("ratelimiter.limited.%s" % user.id)
        raise RateLimited()

    incr_ratelimit(user, domain)
    calls_remaining, time_reset = api_calls_left(user, domain)

    request._ratelimit_remaining = calls_remaining
    request._ratelimit_secs_to_freedom = time_reset

def rate_limit(domain='all'):
    """Rate-limits a view. Takes an optional 'domain' param if you wish to rate limit different
    types of API calls independently.

    Returns a decorator"""
    def wrapper(func):
        @wraps(func)
        def wrapped_func(request, *args, **kwargs):
            # Don't rate limit requests from Django that come from our own servers,
            # and don't rate-limit dev instances
            no_limits = False
            if request.client and request.client.name.lower() == 'internal' and \
               (request.META['REMOTE_ADDR'] in ['::1', '127.0.0.1'] or settings.DEBUG):
                no_limits = True

            if no_limits:
                return func(request, *args, **kwargs)

            try:
                user = request.user
            except:
                user = None

            # Rate-limiting data is stored in redis
            # We also only support rate-limiting authenticated
            # views right now.
            # TODO(leo) - implement per-IP non-authed rate limiting
            if not settings.RATE_LIMITING or not user:
                if not user:
                    logging.error("Requested rate-limiting on %s but user is not authenticated!" % \
                                     func.__name__)
                return func(request, *args, **kwargs)

            rate_limit_user(request, user, domain)

            return func(request, *args, **kwargs)
        return wrapped_func
    return wrapper

def profiled(func):
    """
    This decorator should obviously be used only in a dev environment.
    It works best when surrounding a function that you expect to be
    called once.  One strategy is to write a test case in zerver/tests.py
    and wrap the test case with the profiled decorator.

    You can run a single test case like this:

        # edit zerver/tests.py and place @profiled above the test case below
        ./tools/test-backend zerver.RateLimitTests.test_ratelimit_decrease

    Then view the results like this:

        ./tools/show-profile-results.py test_ratelimit_decrease.profile

    """
    @wraps(func)
    def wrapped_func(*args, **kwargs):
        fn = func.__name__ + ".profile"
        prof = cProfile.Profile()
        retval = prof.runcall(func, *args, **kwargs)
        prof.dump_stats(fn)
        return retval
    return wrapped_func