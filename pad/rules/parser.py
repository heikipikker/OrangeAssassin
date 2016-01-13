"""Parse PAD rule sets.

The general syntax for PAD rules is (on one line):

<type> <name> <value>

Various options can be defined for a rule and they get bundled up using
the name as unique identifier.
"""

from __future__ import absolute_import
from future import standard_library
standard_library.install_hooks()

from builtins import dict
from builtins import object

import re
import contextlib
import collections

import pad.errors
import pad.context
import pad.plugins
import pad.rules.uri
import pad.rules.body
import pad.rules.meta
import pad.rules.full
import pad.rules.eval_
import pad.rules.header
import pad.rules.ruleset

# Simple protection against recursion with "include".
MAX_RECURSION = 10

# Rules that require 2 arguments
KNOWN_2_RTYPE = frozenset(
    (
        "score",  # Specifies the score adjustment if the rule matches
        "describe",  # Specifies a comment describing the rule
        "full",  # Specifies a FullRule
        "body",  # Specifies a BodyRule
        "rawbody",  # Specifies a RawBodyRule
        "uri",  # Specifies a URIRule
        "header",  # Specifies a HeaderRule
        "mimeheader",  # Specifies a MimeHeaderRule
        "meta",  # Specifies a MetaRule
        "eval",  # Specifies a EvalRule
    )
)
# Rules that require 1 arguments
KNOWN_1_RTYPE = frozenset(
    (
        "include",  # Include another file in the current one
        "ifplugin",  # Check if plugin is loaded.
        "loadplugin",  # Load a plugin.
    )
)

# These are the types of rules that we know how to interpret, ignore anything
# else. These include rule types and option types.
KNOWN_RTYPE = KNOWN_1_RTYPE | KNOWN_2_RTYPE

# These types define rules and not options. Map against their specific class.
RULES = {
    "full": pad.rules.full.FullRule,
    "body": pad.rules.body.BodyRule,
    "rawbody": pad.rules.body.RawBodyRule,
    "uri": pad.rules.uri.URIRule,
    "meta": pad.rules.meta.MetaRule,
    "header": pad.rules.header.HeaderRule,
    "mimeheader": pad.rules.header.MimeHeaderRule,
    "eval": pad.rules.eval_.EvalRule,
}

_COMMENT_P = re.compile(r"((?<=[^\\])#.*)")


class PADParser(object):
    """Parses PAD ruleset and extracts and combines the relevant data.

    Note that this is not thread-safe.
    """
    def __init__(self, paranoid=False):
        self.ctxt = pad.context.GlobalContext()
        # XXX This could be a default OrderedDict
        self.results = collections.OrderedDict()
        self.paranoid = paranoid
        self._ignore = False

    @contextlib.contextmanager
    def _paranoid(self, *exceptions):
        """If not paranoid ignore the specified exceptions."""
        try:
            yield
        except exceptions as e:
            self.ctxt.log.error(e)
            if self.paranoid:
                raise

    def parse_file(self, filename, _depth=0):
        """Parses a single PAD ruleset file."""
        if _depth > MAX_RECURSION:
            raise pad.errors.MaxRecursionDepthExceeded()
        self.ctxt.log.debug("Parsing file: %s", filename)

        with open(filename, "rb") as rulef:
            for line_no, line in enumerate(rulef):
                with self._paranoid(pad.errors.InvalidSyntax,
                                    pad.errors.PluginLoadError):
                    self._handle_line(filename, line, line_no + 1, _depth)

    def _handle_line(self, filename, line, line_no, _depth=0):
        """Handles a single line."""
        try:
            line = line.decode("iso-8859-1").strip()
        except UnicodeDecodeError as e:
            raise pad.errors.InvalidSyntax(filename, line_no, line,
                                          "Decoding Error: %s" % e)

        if line.startswith("endif"):
            self._ignore = False
            return

        if not line or line.startswith("#") or self._ignore:
            return

        # Remove any comments
        line = _COMMENT_P.sub("", line).strip()

        try:
            rtype, value = line.split(None, 1)
        except ValueError:
            # Some plugin might know how to handle this line
            rtype, value = line, ""

        if rtype == "include":
            self._handle_include(value, line, line_no, _depth)
        elif rtype == "ifplugin":
            self._handle_ifplugin(value)
        elif rtype == "loadplugin":
            self._handle_loadplugin(value)
        elif rtype in KNOWN_2_RTYPE:
            try:
                rtype, name, value = line.split(None, 2)
            except ValueError:
                raise pad.errors.InvalidSyntax(filename, line_no, line,
                                              "Missing argument")
            if name not in self.results:
                self.results[name] = dict()

            if rtype in RULES:
                if value.startswith("eval:"):
                    # This is for compatibility with SA ruleset
                    self.results[name]["target"] = rtype
                    rtype = "eval"
                self.results[name]["type"] = rtype
                self.results[name]["value"] = value
            else:
                self.results[name][rtype] = value
        else:
            if not self.ctxt.hook_parse_config(rtype, value):
                self.ctxt.log.warning("%s:%s Ignoring unknown configuration "
                                      "line: %s", filename, line_no, line)

    def _handle_include(self, value, line, line_no, _depth=0):
        """Handles the 'include' keyword."""
        filename = value.strip()
        try:
            self.parse_file(filename, _depth=_depth + 1)
        except pad.errors.MaxRecursionDepthExceeded as e:
            e.add_call(filename, line_no, line)
            raise e

    def _handle_ifplugin(self, value):
        """Handles the 'ifplugin' keyword."""
        plugin_name = pad.plugins.REIMPLEMENTED_PLUGINS.get(value, value)
        try:
            plugin_name = plugin_name.rsplit(".", 1)[1]
        except IndexError:
            pass
        if plugin_name not in self.ctxt.plugins:
            self.ctxt.log.debug("Plugin %s not loaded, skipping.", plugin_name)
            self._ignore = True

    def _handle_loadplugin(self, value):
        """Handles the 'loadplugin' keyword."""
        try:
            plugin_name, path = value.split(None, 1)
        except ValueError:
            plugin_name, path = value, None
        if "::" in plugin_name:
            plugin_name = pad.plugins.REIMPLEMENTED_PLUGINS.get(plugin_name)
        if plugin_name:
            self.ctxt.load_plugin(plugin_name, path)
        else:
            self.ctxt.log.warn("Plugin not available: %s", value)

    def get_ruleset(self):
        """Create and return the corresponding ruleset for the parsed files."""
        self.ctxt.hook_parsing_start(self.results)
        ruleset = pad.rules.ruleset.RuleSet(self.ctxt, self.paranoid)
        for name, data in self.results.items():
            try:
                rule_type = data["type"]
            except KeyError:
                e = pad.errors.InvalidRule(name, "No rule type defined.")
                self.ctxt.log.warning(e)
                if self.paranoid:
                    raise e
            else:
                with self._paranoid(pad.errors.InvalidRule, re.error):
                    try:
                        rule_class = RULES[rule_type]
                    except KeyError:
                        # A plugin might have been loaded that
                        # can handle this.
                        rule_class = self.ctxt.cmds[rule_type]
                    self.ctxt.log.debug("Adding rule %s with: %s", name, data)
                    rule = rule_class.get_rule(name, data)
                    ruleset.add_rule(rule)

        self.ctxt.hook_parsing_end(ruleset)
        self.ctxt.log.info("%s rules loaded", len(ruleset.checked))
        ruleset.post_parsing()
        return ruleset


def parse_pad_rules(files, paranoid=False):
    """Parse a list of PAD rules and returns the corresponding ruleset.

    'files' - a list of file paths.

    Returns a dictionary that maps rule names to a dictionary of rule options.
    Every rule will contain "type" and "value" which corresponds to the
    mandatory line for defining a rule. For example (type, name value):

    body LOCAL_DEMONSTRATION_RULE   /test/

    Other options may be included such as "score", "describe".
    """
    parser = PADParser(paranoid=paranoid)
    for filename in files:
        parser.parse_file(filename)

    return parser.get_ruleset()
