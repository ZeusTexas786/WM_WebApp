from django.urls import path
from django.contrib.auth import views as auth_views
from . import accounts, views
from . import health
from . import account_controls
from .cloud.dashboard import dashboard_entry
from .cloud import upload_views

urlpatterns = [
    path('batches/new/',upload_views.new_batch),
    path('batches/sample/',upload_views.sample_batch),
    path('batches/<uuid:batch_id>/',upload_views.batch_page),
    path('batches/<uuid:batch_id>/status/',upload_views.batch_status),
    path('batches/<uuid:batch_id>/uploads/',upload_views.reserve),
    path('batches/<uuid:batch_id>/uploads/<uuid:reservation_id>/',upload_views.resume),
    path('batches/<uuid:batch_id>/uploads/<uuid:reservation_id>/chunk/',upload_views.chunk),
    path('batches/<uuid:batch_id>/uploads/<uuid:reservation_id>/finish/',upload_views.finish),
    path('batches/<uuid:batch_id>/uploads/<uuid:reservation_id>/cancel/',upload_views.cancel_upload),
    path('batches/<uuid:batch_id>/submit/',upload_views.submit),
    path('account/cancel/',account_controls.cancel_account),path('account/reactivate/',account_controls.reactivate),
    path('account/restore/',account_controls.restore),path('account/export/',account_controls.export_account),
    path('sessions/<int:pk>/retry-role/',views.retry_role),
    path('health/live/',health.live),path('health/ready/',health.ready),
    path('candidates/<uuid:pk>/pdf/',views.pdf),
    path('account/',views.account), path('preferences/',views.preference), path('preferences/<int:pk>/delete/',views.preference_delete),
    path('sessions/<int:pk>/sample/',views.sample), path('sessions/<int:pk>/status/',views.status),
    path('candidates/<uuid:pk>/',views.report), path('candidates/<uuid:pk>/evidence/',views.evidence),
    path('candidates/<uuid:pk>/retry/',views.retry), path('candidates/<uuid:pk>/cancel/',views.cancel), path('candidates/<uuid:pk>/interview/',views.interview_request),
    path('sessions/<int:pk>/upload/', views.upload), path('candidates/<uuid:pk>/download/', views.download),
    path('sessions/new/', views.session_new), path('sessions/<int:pk>/', views.session_detail), path('sessions/<int:pk>/confirm/', views.confirm),
    path('', dashboard_entry), path('dashboard/', dashboard_entry, name='dashboard'),
    path('register/', accounts.register), path('login/', accounts.sign_in), path('logout/', accounts.sign_out),
    path('verify-pending/', accounts.pending), path('local-mail/', accounts.local_mail),
    path('verify/<str:token>/', accounts.verify), path('resend/', accounts.resend),
    path('recover/', accounts.RecoveryView.as_view()),
    path('recover/done/', auth_views.PasswordResetDoneView.as_view(template_name='recruiting/recover_done.html')),
    path('reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(template_name='recruiting/reset.html', success_url='/login/'), name='password_reset_confirm'),
]
