#!/usr/bin/env python
import os
from optparse import OptionParser

from jinja2 import Template


TEMPLATE = open(os.path.join(os.path.dirname(__file__), 'crontab.tpl')).read()


def main():
    parser = OptionParser()
    parser.add_option("-k", "--kitsune",
                      help="Location of kitsune (required)")
    parser.add_option("-u", "--user",
                      help=("Prefix cron with this user. "
                           "Only define for cron.d style crontabs"))
    parser.add_option("-p", "--python", default="python",
                      help="Python interpreter to use")

    (opts, args) = parser.parse_args()

    if not opts.kitsune:
        parser.error("-k must be defined")

    ctx = {
        'django': 'cd {0!s}; source virtualenv/bin/activate; {1!s} -W ignore::DeprecationWarning manage.py'.format(
            opts.kitsune, opts.python),
        'scripts': 'cd {0!s}; source virtualenv/bin/activate; {1!s}'.format(
            opts.kitsune, opts.python),
    }
    ctx['cron'] = '{0!s} cron'.format(ctx['django'])
    # Source the venv, don't mess with manage.py
    ctx['rscripts'] = ctx['scripts']

    if opts.user:
        for k, v in ctx.iteritems():
            if k == 'rscripts':
                # rscripts get to run as whatever user is specified in crontab.tpl
                continue
            ctx[k] = '{0!s} {1!s}'.format(opts.user, v)

    # Needs to stay below the opts.user injection.
    ctx['python'] = opts.python

    print Template(TEMPLATE).render(**ctx)


if __name__ == "__main__":
    main()
