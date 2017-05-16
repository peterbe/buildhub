This folder contains several scripts to aggregate build information from different sources and keeping it up to date.

.. notes::

    The ``user:pass`` in the command-line examples is the Basic auth for Kinto.


Scrape archives
===============

Scrape releases on https://archives.mozilla.org and publishes records on a ``archives`` collection.


.. code-block:: bash

    python3 scrape_archives.py --server http://localhost:8888/v1 --auth user:pass --debug


System-Addons updates
=====================

Fetch information about available system addons updates for every Firefox release.

.. code-block:: bash

    python3 sysaddons_update.py --server http://localhost:8888/v1 --auth user:pass --debug



Pulse listener (*WIP*)
======================

Listen to Pulse build and publishes records on a ``builds`` collection.

Obtain Pulse user and password at https://pulseguardian.mozilla.org

.. code-block:: bash

    PULSEGUARDIAN_USER="my-user" PULSEGUARDIAN_PASSWORD="XXX" python2 listen_pulse.py --auth user:pass --debug


TODO
----

* Python 3 everywhere (migrate or get rid of MozillaPulse helper)
