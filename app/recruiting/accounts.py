import hashlib
import secrets
from datetime import timedelta
from functools import wraps
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.views import PasswordResetView
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from .models import Profile, Verification, Throttle, DeletionTombstone

class RegistrationForm(UserCreationForm):
    email = forms.EmailField(max_length=254)
    class Meta:
        model = User
        fields = ('email', 'password1', 'password2')

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if User.objects.filter(username=email).exists():
            raise forms.ValidationError('Unable to register with those details. Try signing in or recovering your account.')
        return email

def limited(request, scope, limit=10):
    # Do not trust client-supplied forwarded IP headers.
    key = hashlib.sha256(f'{scope}:{request.META.get("REMOTE_ADDR", "local")}'.encode()).hexdigest()
    with transaction.atomic():
        record, _ = Throttle.objects.get_or_create(key=key)
        record=Throttle.objects.select_for_update().get(pk=record.pk)
        now = timezone.now()
        if record.window_at < now - timedelta(minutes=15):
            record.count, record.window_at = 0, now
        record.count += 1
        record.save()
        return record.count > limit

def verified_required(view=None, *, track_activity=True):
    if view is None:
        return lambda function: verified_required(function,track_activity=track_activity)
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('/login/')
        profile, _ = Profile.objects.get_or_create(user=request.user)
        if not profile.verified_at:
            return redirect('/verify-pending/')
        with transaction.atomic():
            profile=Profile.objects.select_for_update().get(pk=profile.pk)
            if DeletionTombstone.objects.filter(account_key=profile.account_key).exists():
                return HttpResponse('This account is unavailable.',status=409)
            if profile.status in ('deleting','archiving'):
                return HttpResponse('Your account storage is being maintained. Please try again shortly.',status=409)
            if profile.status=='archived' and request.path not in ('/account/','/account/restore/','/account/cancel/','/account/reactivate/','/account/export/'):
                return redirect('/account/')
            response=view(request,*args,**kwargs)
            if track_activity and profile.status=='active' and response.status_code<400:
                profile.last_activity_at=timezone.now()
                if not profile.cancelled_at: profile.notice_at=None
                profile.save(update_fields=['last_activity_at','notice_at'])
            return response
    return wrapped

def send_verification(request, user):
    token = secrets.token_urlsafe(32)
    Verification.objects.filter(user=user, used_at__isnull=True).update(used_at=timezone.now())
    Verification.objects.create(user=user, digest=hashlib.sha256(token.encode()).hexdigest())
    request.session['verification_delivery'] = 'unconfigured'
    if settings.EMAIL_HOST:
        try:
            send_mail('Verify your WebMonkie account', f'Verify within 24 hours: {settings.PUBLIC_ORIGIN}/verify/{token}/', settings.DEFAULT_FROM_EMAIL, [user.email])
            request.session['verification_delivery'] = 'dispatched'
        except Exception:
            request.session['verification_delivery'] = 'failed'
    elif settings.LOCAL_DEVELOPMENT:
        request.session['local_verification_token'] = token

def verify_token(token):
    with transaction.atomic():
        record = Verification.objects.select_for_update().filter(digest=hashlib.sha256(token.encode()).hexdigest(), used_at__isnull=True, created_at__gte=timezone.now()-timedelta(hours=24)).first()
        if not record:
            return False
        now = timezone.now()
        record.used_at = now
        record.save(update_fields=['used_at'])
        profile, _ = Profile.objects.get_or_create(user=record.user)
        profile=Profile.objects.select_for_update().get(pk=profile.pk)
        if not profile.verified_at:
            profile.verified_at = now
            profile.save(update_fields=['verified_at'])
        return True

def register(request):
    if request.method == 'POST' and limited(request, 'register', 10):
        return HttpResponse('Please try again in 15 minutes.', status=429)
    form = RegistrationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            user = form.save(commit=False)
            user.username = user.email = form.cleaned_data['email']
            user.save()
            Profile.objects.create(user=user)
        login(request, user)
        send_verification(request, user)
        return redirect('/verify-pending/')
    return render(request, 'recruiting/auth.html', {'form': form, 'heading': 'Your next great hire starts here.', 'label': 'Create your workspace', 'button': 'Create account', 'register': True})

def sign_in(request):
    if request.method == 'POST' and limited(request, 'login'):
        return HttpResponse('Too many attempts. Please try again in 15 minutes.', status=429)
    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == 'POST' and form.is_valid():
        login(request, form.get_user())
        return redirect('/dashboard/')
    return render(request, 'recruiting/auth.html', {'form': form, 'heading': 'Welcome back to better hiring.', 'label': 'Sign in to WebMonkie', 'button': 'Sign in'})

@require_POST
def sign_out(request):
    logout(request)
    return redirect('/login/')

def pending(request):
    if not request.user.is_authenticated:
        return redirect('/login/')
    return render(request, 'recruiting/pending.html', {'delivery': request.session.get('verification_delivery'), 'local_token': bool(request.session.get('local_verification_token'))})

def local_mail(request):
    if not settings.LOCAL_DEVELOPMENT or not request.user.is_authenticated or not request.session.get('local_verification_token'):
        raise Http404
    return render(request, 'recruiting/local_mail.html', {'token': request.session['local_verification_token']})

def verify(request, token):
    # GET only previews; link scanners must not consume the token.
    if request.method == 'POST':
        if verify_token(token):
            request.session.pop('local_verification_token', None)
            messages.success(request, 'Verification complete. Your workspace is ready.')
            return redirect('/dashboard/')
        messages.error(request, 'That verification link has expired or was already used.')
        return redirect('/verify-pending/')
    return render(request, 'recruiting/verify.html')

@require_POST
def resend(request):
    if not request.user.is_authenticated:
        return redirect('/login/')
    if limited(request, 'resend', 3):
        return HttpResponse('Please try again in 15 minutes.', status=429)
    send_verification(request, request.user)
    return redirect('/verify-pending/')

class RecoveryView(PasswordResetView):
    template_name='recruiting/recover.html'
    email_template_name='recruiting/reset_email.txt'
    success_url='/recover/done/'

    def post(self,request,*args,**kwargs):
        if limited(request,'recovery',5):
            return HttpResponse('Please try again in 15 minutes.',status=429)
        return super().post(request,*args,**kwargs)

    def form_valid(self,form):
        # Uniform acknowledgement even if SMTP is unavailable or dispatch fails.
        if settings.EMAIL_HOST:
            try: return super().form_valid(form)
            except Exception: pass
        return redirect(self.success_url)
