from django.utils.encoding import smart_str

from kitsune.products.models import Product
from taggit.models import Tag
from kitsune.wiki.models import Document

tags_to_migrate = {
    # source tag -> product
    'firefox': ['firefox'],
    'sync': ['firefox', 'mobile'],
    'persona': ['firefox'],
    'desktop': ['firefox'],
    'fxhome': ['firefox', 'mobile'],
    'firefox-10': ['firefox'],
    'firefox-602': ['firefox'],
    'firefox-50': ['firefox'],
    'android': ['mobile'],
    'mobile': ['mobile']
}


def assert_equals(a, b):
    assert a == b, '{0!s} != {1!s}'.format(a, b)


def run():
    # Get all the tags to migrate.
    tags = list(Tag.objects.filter(slug__in=tags_to_migrate.keys()))

    total_affected = 0

    # For each tag, get the document and add a product for it.
    for tag in tags:
        for product_slug in tags_to_migrate[tag.slug]:
            product = Product.objects.get(slug=product_slug)

            # Assign the product to all the documents tagged with tag.
            for doc in Document.objects.filter(tags__slug=tag.slug):

                doc.products.add(product)

                print 'Added product "{0!s}" to document "{1!s}"'.format(
                    smart_str(product.slug), smart_str(doc.title))
                total_affected += 1

    print 'Done! ({0:d})'.format(total_affected)
