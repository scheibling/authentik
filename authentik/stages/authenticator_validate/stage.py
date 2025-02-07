"""Authenticator Validation"""
from datetime import timezone

from django.http import HttpRequest, HttpResponse
from django.utils.timezone import datetime, now
from django_otp import devices_for_user
from django_otp.models import Device
from rest_framework.fields import CharField, IntegerField, JSONField, ListField, UUIDField
from rest_framework.serializers import ValidationError
from structlog.stdlib import get_logger

from authentik.core.api.utils import PassiveSerializer
from authentik.core.models import User
from authentik.events.models import Event, EventAction
from authentik.events.utils import cleanse_dict, sanitize_dict
from authentik.flows.challenge import ChallengeResponse, ChallengeTypes, WithUserInfoChallenge
from authentik.flows.exceptions import FlowSkipStageException
from authentik.flows.models import FlowDesignation, NotConfiguredAction, Stage
from authentik.flows.planner import PLAN_CONTEXT_PENDING_USER
from authentik.flows.stage import ChallengeStageView
from authentik.lib.utils.time import timedelta_from_string
from authentik.stages.authenticator_sms.models import SMSDevice
from authentik.stages.authenticator_validate.challenge import (
    DeviceChallenge,
    get_challenge_for_device,
    get_webauthn_challenge_userless,
    select_challenge,
    validate_challenge_code,
    validate_challenge_duo,
    validate_challenge_webauthn,
)
from authentik.stages.authenticator_validate.models import AuthenticatorValidateStage, DeviceClasses
from authentik.stages.authenticator_webauthn.models import WebAuthnDevice
from authentik.stages.password.stage import PLAN_CONTEXT_METHOD, PLAN_CONTEXT_METHOD_ARGS

LOGGER = get_logger()
SESSION_STAGES = "goauthentik.io/stages/authenticator_validate/stages"
SESSION_SELECTED_STAGE = "goauthentik.io/stages/authenticator_validate/selected_stage"
SESSION_DEVICE_CHALLENGES = "goauthentik.io/stages/authenticator_validate/device_challenges"


class SelectableStageSerializer(PassiveSerializer):
    """Serializer for stages which can be selected by users"""

    pk = UUIDField()
    name = CharField()
    verbose_name = CharField()
    meta_model_name = CharField()


class AuthenticatorValidationChallenge(WithUserInfoChallenge):
    """Authenticator challenge"""

    device_challenges = ListField(child=DeviceChallenge())
    component = CharField(default="ak-stage-authenticator-validate")
    configuration_stages = ListField(child=SelectableStageSerializer())


class AuthenticatorValidationChallengeResponse(ChallengeResponse):
    """Challenge used for Code-based and WebAuthn authenticators"""

    selected_challenge = DeviceChallenge(required=False)
    selected_stage = CharField(required=False)

    code = CharField(required=False)
    webauthn = JSONField(required=False)
    duo = IntegerField(required=False)
    component = CharField(default="ak-stage-authenticator-validate")

    def _challenge_allowed(self, classes: list):
        device_challenges: list[dict] = self.stage.request.session.get(SESSION_DEVICE_CHALLENGES)
        if not any(x["device_class"] in classes for x in device_challenges):
            raise ValidationError("No compatible device class allowed")

    def validate_code(self, code: str) -> str:
        """Validate code-based response, raise error if code isn't allowed"""
        self._challenge_allowed([DeviceClasses.TOTP, DeviceClasses.STATIC, DeviceClasses.SMS])
        return validate_challenge_code(code, self.stage.request, self.stage.get_pending_user())

    def validate_webauthn(self, webauthn: dict) -> dict:
        """Validate webauthn response, raise error if webauthn wasn't allowed
        or response is invalid"""
        self._challenge_allowed([DeviceClasses.WEBAUTHN])
        return validate_challenge_webauthn(
            webauthn, self.stage.request, self.stage.get_pending_user()
        )

    def validate_duo(self, duo: int) -> int:
        """Initiate Duo authentication"""
        self._challenge_allowed([DeviceClasses.DUO])
        return validate_challenge_duo(duo, self.stage.request, self.stage.get_pending_user())

    def validate_selected_challenge(self, challenge: dict) -> dict:
        """Check which challenge the user has selected. Actual logic only used for SMS stage."""
        # First check if the challenge is valid
        allowed = False
        for device_challenge in self.stage.request.session.get(SESSION_DEVICE_CHALLENGES):
            if device_challenge.get("device_class", "") == challenge.get(
                "device_class", ""
            ) and device_challenge.get("device_uid", "") == challenge.get("device_uid", ""):
                allowed = True
        if not allowed:
            raise ValidationError("invalid challenge selected")

        if challenge.get("device_class", "") != "sms":
            return challenge
        devices = SMSDevice.objects.filter(pk=int(challenge.get("device_uid", "0")))
        if not devices.exists():
            raise ValidationError("invalid challenge selected")
        select_challenge(self.stage.request, devices.first())
        return challenge

    def validate_selected_stage(self, stage_pk: str) -> str:
        """Check that the selected stage is valid"""
        stages = self.stage.request.session.get(SESSION_STAGES, [])
        if not any(str(stage.pk) == stage_pk for stage in stages):
            raise ValidationError("Selected stage is invalid")
        LOGGER.debug("Setting selected stage to ", stage=stage_pk)
        self.stage.request.session[SESSION_SELECTED_STAGE] = stage_pk
        return stage_pk

    def validate(self, attrs: dict):
        # Checking if the given data is from a valid device class is done above
        # Here we only check if the any data was sent at all
        if "code" not in attrs and "webauthn" not in attrs and "duo" not in attrs:
            raise ValidationError("Empty response")
        return attrs


def get_device_last_usage(device: Device) -> datetime:
    """Get a datetime object from last_t"""
    if not hasattr(device, "last_t"):
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if isinstance(device.last_t, datetime):
        return device.last_t
    return datetime.fromtimestamp(device.last_t * device.step, tz=timezone.utc)


class AuthenticatorValidateStageView(ChallengeStageView):
    """Authenticator Validation"""

    response_class = AuthenticatorValidationChallengeResponse

    def get_device_challenges(self) -> list[dict]:
        """Get a list of all device challenges applicable for the current stage"""
        challenges = []
        # Convert to a list to have usable log output instead of just <generator ...>
        user_devices = list(devices_for_user(self.get_pending_user()))
        LOGGER.debug("Got devices for user", devices=user_devices)

        # static and totp are only shown once
        # since their challenges are device-independant
        seen_classes = []

        stage: AuthenticatorValidateStage = self.executor.current_stage

        _now = now()
        threshold = timedelta_from_string(stage.last_auth_threshold)

        for device in user_devices:
            device_class = device.__class__.__name__.lower().replace("device", "")
            if device_class not in stage.device_classes:
                LOGGER.debug("device class not allowed", device_class=device_class)
                continue
            # Ensure only one challenge per device class
            # WebAuthn does another device loop to find all webuahtn devices
            if device_class in seen_classes:
                continue
            # check if device has been used within threshold and skip this stage if so
            if threshold.total_seconds() > 0:
                print("yeet")
                print(get_device_last_usage(device))
                print(_now - get_device_last_usage(device))
                print(threshold)
                print(_now - get_device_last_usage(device) <= threshold)
                if _now - get_device_last_usage(device) <= threshold:
                    LOGGER.info("Device has been used within threshold", device=device)
                    raise FlowSkipStageException()
            if device_class not in seen_classes:
                seen_classes.append(device_class)
            challenge = DeviceChallenge(
                data={
                    "device_class": device_class,
                    "device_uid": device.pk,
                    "challenge": get_challenge_for_device(self.request, device),
                }
            )
            challenge.is_valid()
            challenges.append(challenge.data)
            LOGGER.debug("adding challenge for device", challenge=challenge)
        return challenges

    def get_userless_webauthn_challenge(self) -> list[dict]:
        """Get a WebAuthn challenge when no pending user is set."""
        challenge = DeviceChallenge(
            data={
                "device_class": DeviceClasses.WEBAUTHN,
                "device_uid": -1,
                "challenge": get_webauthn_challenge_userless(self.request),
            }
        )
        challenge.is_valid()
        return [challenge.data]

    # pylint: disable=too-many-return-statements
    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        """Check if a user is set, and check if the user has any devices
        if not, we can skip this entire stage"""
        user = self.get_pending_user()
        stage: AuthenticatorValidateStage = self.executor.current_stage
        if user and not user.is_anonymous:
            try:
                challenges = self.get_device_challenges()
            except FlowSkipStageException:
                return self.executor.stage_ok()
        else:
            if self.executor.flow.designation != FlowDesignation.AUTHENTICATION:
                LOGGER.debug("Refusing passwordless flow in non-authentication flow")
                return self.executor.stage_ok()
            # Passwordless auth, with just webauthn
            if DeviceClasses.WEBAUTHN in stage.device_classes:
                LOGGER.debug("Userless flow, getting generic webauthn challenge")
                challenges = self.get_userless_webauthn_challenge()
            else:
                LOGGER.debug("No pending user, continuing")
                return self.executor.stage_ok()
        self.request.session[SESSION_DEVICE_CHALLENGES] = challenges

        # No allowed devices
        if len(challenges) < 1:
            if stage.not_configured_action == NotConfiguredAction.SKIP:
                LOGGER.debug("Authenticator not configured, skipping stage")
                return self.executor.stage_ok()
            if stage.not_configured_action == NotConfiguredAction.DENY:
                LOGGER.debug("Authenticator not configured, denying")
                return self.executor.stage_invalid()
            if stage.not_configured_action == NotConfiguredAction.CONFIGURE:
                LOGGER.debug("Authenticator not configured, forcing configure")
                return self.prepare_stages(user)
        return super().get(request, *args, **kwargs)

    def prepare_stages(self, user: User, *args, **kwargs) -> HttpResponse:
        """Check how the user can configure themselves. If no stages are set, return an error.
        If a single stage is set, insert that stage directly. If multiple are selected, include
        them in the challenge."""
        stage: AuthenticatorValidateStage = self.executor.current_stage
        if not stage.configuration_stages.exists():
            Event.new(
                EventAction.CONFIGURATION_ERROR,
                message=(
                    "Authenticator validation stage is set to configure user "
                    "but no configuration flow is set."
                ),
                stage=self,
            ).from_http(self.request).set_user(user).save()
            return self.executor.stage_invalid()
        if stage.configuration_stages.count() == 1:
            next_stage = Stage.objects.get_subclass(pk=stage.configuration_stages.first().pk)
            LOGGER.debug("Single stage configured, auto-selecting", stage=next_stage)
            self.request.session[SESSION_SELECTED_STAGE] = next_stage
            # Because that normal insetion only happens on post, we directly inject it here and
            # return it
            self.executor.plan.insert_stage(next_stage)
            return self.executor.stage_ok()
        stages = Stage.objects.filter(pk__in=stage.configuration_stages.all()).select_subclasses()
        self.request.session[SESSION_STAGES] = stages
        return super().get(self.request, *args, **kwargs)

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        res = super().post(request, *args, **kwargs)
        if (
            SESSION_SELECTED_STAGE in self.request.session
            and self.executor.current_stage.not_configured_action == NotConfiguredAction.CONFIGURE
        ):
            LOGGER.debug("Got selected stage in session, running that")
            stage_pk = self.request.session.get(SESSION_SELECTED_STAGE)
            # Because the foreign key to stage.configuration_stage points to
            # a base stage class, we need to do another lookup
            stage = Stage.objects.get_subclass(pk=stage_pk)
            # plan.insert inserts at 1 index, so when stage_ok pops 0,
            # the configuration stage is next
            self.executor.plan.insert_stage(stage)
            return self.executor.stage_ok()
        return res

    def get_challenge(self) -> AuthenticatorValidationChallenge:
        challenges = self.request.session.get(SESSION_DEVICE_CHALLENGES, [])
        stages = self.request.session.get(SESSION_STAGES, [])
        stage_challenges = []
        for stage in stages:
            serializer = SelectableStageSerializer(
                data={
                    "pk": stage.pk,
                    "name": stage.name,
                    "verbose_name": str(stage._meta.verbose_name),
                    "meta_model_name": f"{stage._meta.app_label}.{stage._meta.model_name}",
                }
            )
            serializer.is_valid()
            stage_challenges.append(serializer.data)
        return AuthenticatorValidationChallenge(
            data={
                "type": ChallengeTypes.NATIVE.value,
                "device_challenges": challenges,
                "configuration_stages": stage_challenges,
            }
        )

    # pylint: disable=unused-argument
    def challenge_valid(self, response: AuthenticatorValidationChallengeResponse) -> HttpResponse:
        # All validation is done by the serializer
        user = self.executor.plan.context.get(PLAN_CONTEXT_PENDING_USER)
        if not user:
            webauthn_device: WebAuthnDevice = response.data.get("webauthn", None)
            if not webauthn_device:
                return self.executor.stage_ok()
            LOGGER.debug("Set user from userless flow", user=webauthn_device.user)
            self.executor.plan.context[PLAN_CONTEXT_PENDING_USER] = webauthn_device.user
            self.executor.plan.context[PLAN_CONTEXT_METHOD] = "auth_webauthn_pwl"
            self.executor.plan.context[PLAN_CONTEXT_METHOD_ARGS] = cleanse_dict(
                sanitize_dict(
                    {
                        "device": webauthn_device,
                    }
                )
            )
        return self.executor.stage_ok()
