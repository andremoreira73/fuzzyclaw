from django import forms
from django.contrib.auth.models import User


class ProfileForm(forms.ModelForm):
    """Edit first name, last name, and email. Username is read-only (admin-managed)."""

    username = forms.CharField(
        disabled=True,
        help_text='Managed by administrator.',
    )

    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email']
