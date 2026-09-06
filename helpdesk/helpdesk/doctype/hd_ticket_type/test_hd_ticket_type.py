# -*- coding: utf-8 -*-
# Copyright (c) 2017, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt
from __future__ import unicode_literals

import unittest

from helpdesk.test_utils import ignored_test_record_dependencies

IGNORE_TEST_RECORD_DEPENDENCIES = ignored_test_record_dependencies("HD Ticket Type")


class TestHDTicketType(unittest.TestCase):
    pass
