import logging

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib.auth.models import User
from django.http import HttpResponseForbidden
from django.template.loader import render_to_string

from meals.models import AllowedUser

logger = logging.getLogger(__name__)


class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):
    def on_authentication_error(self, request, provider, error=None, exception=None, extra_context=None):
        logger.error(
            "Social login authentication error: provider=%s error=%s exception=%r extra_context=%s",
            provider, error, exception, extra_context,
        )

    def pre_social_login(self, request, sociallogin):
        email = sociallogin.account.extra_data.get('email', '')
        if not AllowedUser.objects.filter(email__iexact=email).exists():
            raise ImmediateHttpResponse(
                HttpResponseForbidden(render_to_string("forbidden.html", request=request))
            )

        user = sociallogin.user
        if user.id:
            return

        user.email = email
        user.username = email.split('@')[0].lower()
        user.first_name = sociallogin.account.extra_data.get('given_name', '')

        try:
            existing_user = User.objects.get(email__iexact=email)
            sociallogin.connect(request, existing_user)
            return
        except User.DoesNotExist:
            pass

        sociallogin.save(request)


class CustomAccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return False
