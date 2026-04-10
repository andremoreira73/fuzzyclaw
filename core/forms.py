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

    def clean_email(self):
        email = self.cleaned_data['email']
        if email and User.objects.filter(email=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('This email address is already in use.')
        return email
