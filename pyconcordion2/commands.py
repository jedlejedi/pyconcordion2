from __future__ import unicode_literals
from collections import OrderedDict
import imp
import inspect
from io import BytesIO
import os
import re
import traceback
import unittest

from lxml import etree
from lxml import html

import expression_parser

truth_values = ['true', '1', 't', 'y', 'yes']

CONCORDION_NAMESPACE = "http://www.concordion.org/2007/concordion"


class ResultEvent(object):
    def __init__(self, actual, expected):
        self.actual = actual
        self.expected = expected


class Result(object):
    def __init__(self, tree):
        self.root_element = tree
        self.successes = tree.xpath("//*[contains(concat(' ', @class, ' '), ' success ')]")
        self.failures = tree.xpath("//*[contains(concat(' ', @class, ' '), ' failure ')]")
        self.missing = tree.xpath("//*[contains(concat(' ', @class, ' '), ' missing ')]")
        self.exceptions = tree.xpath("//*[contains(concat(' ', @class, ' '), ' exceptionMessage ')]")

    def last_failed_event(self):
        last_failed = self.failures[-1]
        actual = last_failed.xpath("//*[@class='actual']")[0].text
        expected = last_failed.xpath("//*[@class='expected']")[0].text
        return ResultEvent(actual, expected)

    @property
    def failureCount(self):
        return len(self.failures)

    @property
    def exceptionCount(self):
        return len(self.exceptions)

    @property
    def successCount(self):
        return len(self.successes)

    def has_failed(self):
        return bool(self.failureCount or self.exceptionCount)

    def has_succeeded(self):
        return not self.has_failed()


class Commander(object):
    def __init__(self, test, filename):
        self.test = test
        self.filename = filename
        self.tree = etree.parse(self.filename)
        self.args = {}
        self.commands = OrderedDict()

    def process(self):
        """
        1. Finds all concordion elements
        2. Iterates over concordion attributes
        3. Generates ordered dictionary of commands
        4. Executes commands in order
        """
        elements = self.__find_concordion_elements()

        for element in elements:
            for key, expression_str in element.attrib.items():
                if CONCORDION_NAMESPACE not in key:  # we ignore any attributes that are not concordion
                    continue

                key = key.replace("{%s}" % CONCORDION_NAMESPACE, "")  # we remove the namespace

                command_cls = command_mapper.get(key)
                command = command_cls(element, expression_str, self.test)

                if element.tag.lower() == "th":
                    index = self.__find_th_index(element)
                    command.index = index

                self.__add_to_commands_dict(command)

        self.__run_commands()
        self.__postprocess_tree()
        self.result = Result(self.tree)

    def __find_concordion_elements(self):
        """
        Retrieves all etree elements with the concordion namespace
        """
        return self.tree.xpath("""//*[namespace-uri()='{namespace}' or @*[namespace-uri()='{namespace}']]""".format(
            namespace=CONCORDION_NAMESPACE))

    def __find_th_index(self, element):
        """
        Returns the index of the given table header cell
        """
        parent = element.getparent()
        for index, th_element in enumerate(parent.xpath("th")):
            if th_element == element:
                return index
        raise RuntimeError("Could not match command with table header")  # should NEVER happen

    def __add_to_commands_dict(self, command):
        """
        Given a command, we check to see if it's a child of another command. If it is we add it to the list of child
        commands. Otherwise we set it as a brand new command
        """
        element = command.element
        while element.getparent() is not None:
            if element.getparent() in self.commands:
                self.commands[element.getparent()].children.append(command)
                return
            else:
                element = element.getparent()
        self.commands[command.element] = command

    def __run_commands(self):
        """
        Runs each command in order
        """
        for element, command in self.commands.items():
            command.run()

    def __postprocess_tree(self):
        css_path = os.path.join(os.path.dirname(__file__), "resources", "css", "embedded.css")
        css_contents = open(css_path, "rU").read()

        jquery_path = os.path.join(os.path.dirname(__file__), "resources", "js", "jquery-1.9.1.min.js")
        js_path = os.path.join(os.path.dirname(__file__), "resources", "js", "main.js")

        meta = etree.Element("meta")
        meta.attrib["http-equiv"] = "content-type"
        meta.attrib["content"] = "text/html; charset=UTF-8"
        meta.tail = "\n"

        head = self.tree.xpath("//head")
        if head:
            head[0].insert(0, meta)
        else:
            head = etree.Element("head")
            head.text = "\n"
            head.append(meta)

            for child in self.tree.getroot().getchildren():
                if child.tag == "body":
                    break
                head.append(child)
            head.tail = "\n"

            self.tree.getroot().insert(0, head)

        head = self.tree.xpath("//head")[0]
        style_tag = etree.Element("style", type="text/css")
        style_tag.text = css_contents
        head.append(style_tag)

        js_tag = etree.Element("script", src=js_path)
        js_tag.text = " "
        jquery_tag = etree.Element("script", src=jquery_path)
        jquery_tag.text = " "

        self.tree.getroot().append(jquery_tag)
        self.tree.getroot().append(js_tag)


class Command(object):
    def __init__(self, element, expression_str, context):
        self.element = element
        self.expression_str = expression_str.replace("#", "")
        self.context = context
        self.children = []

    def _run(self):
        raise NotImplementedError

    def run(self):
        try:
            self.context.TEXT = get_element_content(self.element)
            self._run()
            return True
        except Exception as e:
            mark_exception(self.element, e)


class RunCommand(Command):
    def _run(self):
        href = self.element.attrib["href"].replace(".html", "")
        f = inspect.getfile(self.context.__class__)
        file_path = os.path.join(os.path.dirname(os.path.abspath(f)), href)
        try:
            src_file_path = file_path + ".py"
            test_class = imp.load_source("Test", src_file_path)
        except Exception:
            src_file_path = file_path + "Test.py"
            test_class = imp.load_source("Test", src_file_path)

        root, ext = os.path.splitext(os.path.basename(src_file_path))

        test_class = getattr(test_class, root)()
        test_class.extra_folder = os.path.dirname(href)
        result = unittest.TextTestRunner().run(test_class)
        if result.failures or result.errors:
            mark_status(False, self.element)
        else:
            mark_status(True, self.element)


class ExecuteCommand(Command):
    def _run(self):
        if self.element.tag.lower() == "table":
            for row in get_table_body_rows(self.element):
                for command in self.children:
                    td_element = row.xpath("td")[command.index]
                    command.element = td_element
                self._run_children()
        else:
            self._run_children()

    def _run_children(self):
        for command in self.children:
            if isinstance(command, SetCommand):
                command.run()
        expression_parser.execute_within_context(self.context, self.expression_str)
        for command in self.children:
            if not isinstance(command, SetCommand):
                command.run()


class VerifyRowsCommand(Command):
    def _run(self):
        variable_name = expression_parser.parse(self.expression_str).variable_name
        results = expression_parser.execute_within_context(self.context, self.expression_str)
        for result, row in zip(results, get_table_body_rows(self.element)):
            setattr(self.context, variable_name, result)
            for command in self.children:
                element = row.xpath("td")[command.index]
                command.element = element
                command.run()


def get_table_body_rows(table):
    tr_s = table.xpath("tr")
    return [tr for tr in tr_s if tr.xpath("td")]


def normalize(text):
    text = unicode(text)
    text = text.replace(" _\n", "")  # support for python style line breaks
    pattern = re.compile(r'\s+')  # treat all whitespace as spaces
    return re.sub(pattern, ' ', text).strip()


def get_element_content(element):
    tag_html = html.parse(BytesIO(etree.tostring(element))).getroot().getchildren()[0].getchildren()[0]
    return normalize(tag_html.text_content())


class SetCommand(Command):
    def _run(self):
        expression = expression_parser.parse(self.expression_str)
        if expression.function_name:  # concordion:set="blah = function(#TEXT)"
            expression_parser.execute_within_context(self.context, self.expression_str)
        else:
            setattr(self.context, expression.variable_name, get_element_content(self.element))


class AssertEqualsCommand(Command):
    def _run(self):
        expression_return = expression_parser.execute_within_context(self.context, self.expression_str)
        if expression_return is None:
            expression_return = "(None)"

        result = normalize(expression_return) == get_element_content(self.element)
        if result:
            mark_status(result, self.element)
        else:
            mark_status(result, self.element, expression_return)


class AssertTrueCommand(Command):
    def _run(self):
        result = expression_parser.execute_within_context(self.context, self.expression_str)
        mark_status(result, self.element, "== false")


class AssertFalseCommand(Command):
    def _run(self):
        result = expression_parser.execute_within_context(self.context, self.expression_str)
        mark_status(not result, self.element, "== true")


class EchoCommand(Command):
    def _run(self):
        result = expression_parser.execute_within_context(self.context, self.expression_str)
        if result is not None:
            self.element.text = result
        else:
            em = etree.Element("em")
            em.text = "None"
            self.element.append(em)


def mark_status(is_successful, element, actual_value=None):
    if not get_element_content(element):  # set non-breaking space if element is empty
        element.text = "\u00A0"

    if is_successful:
        element.attrib["class"] = (element.attrib.get("class", "") + " success").strip()
    else:
        element.attrib["class"] = (element.attrib.get("class", "") + " failure").strip()

        actual = etree.Element("ins", **{"class": "actual"})
        actual.text = unicode(actual_value or "\u00A0")  # blank space if no value

        # we move child elements from element into our new del container
        expected = etree.Element("del", **{"class": "expected"})
        for child in element.getchildren():
            expected.append(child)
        expected.text = element.text
        element.text = None

        element.insert(0, expected)
        element.insert(1, actual)


__exception_index = 1


def mark_exception(target_element, e):
    global __exception_index
    exception_element = etree.Element("span", **{"class": "exceptionMessage"})
    exception_element.text = unicode(e)

    input_element = etree.Element("input",
                                  **{"class": "stackTraceButton", "data-exception-index": unicode(__exception_index),
                                     "type": "button", "value": "Toggle Stack"})

    stacktrace_div_element = etree.Element("div", **{"class": "stackTrace {}".format(__exception_index)})
    p_tag = etree.Element("p")
    p_tag.text = "Traceback:"
    stacktrace_div_element.append(p_tag)
    tb = traceback.format_exc()
    for line in tb.splitlines():
        trace_element = etree.Element("div", **{"class": "stackTraceEntry"})
        trace_element.text = line
        stacktrace_div_element.append(trace_element)

    parent = target_element.getparent()
    # we insert the exception after the element in question
    for i, element in enumerate((exception_element, input_element, stacktrace_div_element)):
        parent.insert(parent.index(target_element) + 1 + i, element)

    __exception_index += 1


command_mapper = {
    "run": RunCommand,
    "execute": ExecuteCommand,
    "set": SetCommand,
    "assertEquals": AssertEqualsCommand,
    "assertTrue": AssertTrueCommand,
    "assertFalse": AssertFalseCommand,
    "verifyRows": VerifyRowsCommand,
    "echo": EchoCommand
}
