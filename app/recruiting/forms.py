from django import forms
from django.conf import settings
from .models import HiringSession

class SessionForm(forms.ModelForm):
    mode = forms.ChoiceField(choices=[('live', 'Live AI · requires an API key'), ('sample', 'Sample walkthrough · synthetic demo only')])
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        if not settings.AI_API_KEY: self.fields['mode'].initial='sample'
    class Meta:
        model = HiringSession
        fields = ['title', 'jd', 'mode']
        labels = {'title': 'Role / session name', 'jd': 'Job description'}
        widgets = {'title': forms.TextInput(attrs={'placeholder': 'e.g. Senior Python Engineer'}), 'jd': forms.Textarea(attrs={'placeholder': 'Paste the responsibilities, required skills, and experience…', 'maxlength': 30000})}

    def clean_jd(self):
        jd = self.cleaned_data['jd'].strip()
        if len(jd) > 30000:
            raise forms.ValidationError('Please keep the JD under 30,000 characters.')
        return jd
