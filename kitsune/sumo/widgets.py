# Based on http://djangosnippets.org/snippets/1580/
from django import forms


class ImageWidget(forms.FileInput):
    """
    A ImageField Widget that shows a thumbnail.
    """

    def __init__(self, attrs=None):
        if attrs is None:
            attrs = {}
        super(ImageWidget, self).__init__(attrs)

    def render(self, name, value, attrs=None):
        output = super(ImageWidget, self).render(name, value, attrs)
        if value and hasattr(value, 'url'):
            output = ('<div class="val-wrap"><img src="{0!s}" alt="" />{1!s}</div>'.format(value.url, output))
        return output
