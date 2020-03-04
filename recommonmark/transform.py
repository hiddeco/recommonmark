"""Implement some common transforms on parsed AST."""

import os
import re

from docutils import nodes, transforms
from docutils.statemachine import StringList
from docutils.parsers.rst import Parser
from docutils.utils import new_document
from sphinx import addnodes

from .states import DummyStateMachine


class AutoStructify(transforms.Transform):

    """Automatically try to transform blocks to sphinx directives.

    This class is designed to handle AST generated by CommonMarkParser.
    """

    def __init__(self, *args, **kwargs):
        transforms.Transform.__init__(self, *args, **kwargs)
        self.reporter = self.document.reporter
        self.config = self.default_config.copy()
        try:
            new_cfg = self.document.settings.env.config.recommonmark_config
            self.config.update(new_cfg)
        except AttributeError:
            pass

        # Deprecation notices
        # TODO move this check to an extension pattern, and only call once
        if self.config.get('enable_auto_doc_ref', False):
            self.reporter.warning(
                'AutoStructify option "enable_auto_doc_ref" is deprecated')

    # set to a high priority so it can be applied first for markdown docs
    default_priority = 1
    suffix_set = set(['md', 'rst'])

    default_config = {
        'enable_auto_doc_ref': False,
        'auto_toc_maxdepth': 1,
        'auto_toc_tree_section': None,
        'enable_auto_toc_tree': True,
        'enable_eval_rst': True,
        'enable_math': True,
        'enable_inline_math': True,
        'commonmark_suffixes': ['.md'],
        'url_resolver': lambda x: x,
        'known_url_schemes': None,
    }

    def parse_ref(self, ref):
        """Analyze the ref block, and return the information needed.

        Parameters
        ----------
        ref : nodes.reference

        Returns
        -------
        result : tuple of (str, str, str)
            The returned result is tuple of (title, uri, docpath).
            title is the display title of the ref.
            uri is the html uri of to the ref after resolve.
            docpath is the absolute document path to the document, if
            the target corresponds to an internal document, this can bex None
        """
        title = None
        if len(ref.children) == 0:
            title = ref['name'] if 'name' in ref else None
        elif isinstance(ref.children[0], nodes.Text):
            title = ref.children[0].astext()
        uri = ref['refuri']
        if uri.find('://') != -1:
            return (title, uri, None)
        anchor = None
        arr = uri.split('#')
        if len(arr) == 2:
            anchor = arr[1]
        if len(arr) > 2 or len(arr[0]) == 0:
            return (title, uri, None)
        uri = arr[0]

        abspath = os.path.abspath(os.path.join(self.file_dir, uri))
        relpath = os.path.relpath(abspath, self.root_dir)
        suffix = abspath.rsplit('.', 1)
        if len(suffix) == 2 and suffix[1] in AutoStructify.suffix_set and (
                os.path.exists(abspath) and abspath.startswith(self.root_dir)):
            # replace the path separator if running on non-UNIX environment
            if os.path.sep != '/':
                relpath = relpath.replace(os.path.sep, '/')
            docpath = '/' + relpath.rsplit('.', 1)[0]
            # rewrite suffix to html, this is suboptimal
            uri = docpath + '.html'
            if anchor is None:
                return (title, uri, docpath)
            else:
                return (title, uri + '#' + anchor, None)
        else:
            # use url resolver
            if self.url_resolver:
                uri = self.url_resolver(relpath)
            if anchor:
                uri += '#' + anchor
            return (title, uri, None)

    def auto_toc_tree(self, node):  # pylint: disable=too-many-branches
        """Try to convert a list block to toctree in rst.

        This function detects if the matches the condition and return
        a converted toc tree node. The matching condition:
        The list only contains one level, and only contains references

        Parameters
        ----------
        node: nodes.Sequential
            A list node in the doctree

        Returns
        -------
        tocnode: docutils node
            The converted toc tree node, None if conversion is not possible.
        """
        if not self.config['enable_auto_toc_tree']:
            return None
        # when auto_toc_tree_section is set
        # only auto generate toctree under the specified section title
        sec = self.config['auto_toc_tree_section']
        if sec is not None:
            if node.parent is None:
                return None
            title = None
            if isinstance(node.parent, nodes.section):
                child = node.parent.first_child_matching_class(nodes.title)
                if child is not None:
                    title = node.parent.children[child]
            elif isinstance(node.parent, nodes.paragraph):
                child = node.parent.parent.first_child_matching_class(nodes.title)
                if child is not None:
                    title = node.parent.parent.children[child]
            if not title:
                return None
            if title.astext().strip() != sec:
                return None

        numbered = None
        if isinstance(node, nodes.bullet_list):
            numbered = 0
        elif isinstance(node, nodes.enumerated_list):
            numbered = 1

        if numbered is None:
            return None
        refs = []
        for nd in node.children[:]:
            assert isinstance(nd, nodes.list_item)
            if len(nd.children) != 1:
                return None
            par = nd.children[0]
            if not isinstance(par, nodes.paragraph):
                return None
            if len(par.children) != 1:
                return None
            ref = par.children[0]
            if isinstance(ref, addnodes.pending_xref):
                ref = ref.children[0]
            if not isinstance(ref, nodes.reference):
                return None
            title, uri, docpath = self.parse_ref(ref)
            # parse_ref() produces an absolute path
            # while we need a relative path here
            docpath = ref['refuri']
            if title is None or uri.startswith('#'):
                return None
            if docpath:
                refs.append((title, docpath))
            else:
                refs.append((title, uri))
        self.state_machine.reset(self.document,
                                 node.parent,
                                 self.current_level)
        return self.state_machine.run_directive(
            'toctree',
            options={
                'maxdepth': self.config['auto_toc_maxdepth'],
                'numbered': numbered,
            },
            content=['%s <%s>' % (k, v) for k, v in refs])

    def auto_inline_code(self, node):
        """Try to automatically generate nodes for inline literals.

        Parameters
        ----------
        node : nodes.literal
            Original codeblock node
        Returns
        -------
        tocnode: docutils node
            The converted toc tree node, None if conversion is not possible.
        """
        assert isinstance(node, nodes.literal)
        if len(node.children) != 1:
            return None
        content = node.children[0]
        if not isinstance(content, nodes.Text):
            return None
        content = content.astext().strip()
        if content.startswith('$') and content.endswith('$'):
            if not self.config['enable_inline_math']:
                return None
            content = content[1:-1]
            self.state_machine.reset(self.document,
                                     node.parent,
                                     self.current_level)
            return self.state_machine.run_role('math', content=content)
        else:
            return None

    def auto_code_block(self, node):
        """Try to automatically generate nodes for codeblock syntax.

        Parameters
        ----------
        node : nodes.literal_block
            Original codeblock node
        Returns
        -------
        tocnode: docutils node
            The converted toc tree node, None if conversion is not possible.
        """
        assert isinstance(node, nodes.literal_block)
        original_node = node
        if 'language' not in node:
            return None
        self.state_machine.reset(self.document,
                                 node.parent,
                                 self.current_level)
        content = node.rawsource.split('\n')
        language = node['language']
        if language == 'math':
            if self.config['enable_math']:
                return self.state_machine.run_directive(
                    'math', content=content)
        elif language == 'eval_rst':
            if self.config['enable_eval_rst']:
                # allow embed non section level rst
                node = nodes.section()
                self.state_machine.state.nested_parse(
                    StringList(content, source=original_node.source),
                    0, node=node, match_titles=True)
                return node.children[:]
        else:
            match = re.search('[ ]?[\w_-]+::.*', language)
            if match:
                parser = Parser()
                new_doc = new_document(None, self.document.settings)
                newsource = u'.. ' + match.group(0) + '\n' + node.rawsource
                parser.parse(newsource, new_doc)
                return new_doc.children[:]
            else:
                return self.state_machine.run_directive(
                    'code-block', arguments=[language],
                    content=content)
        return None

    def find_replace(self, node):
        """Try to find replace node for current node.

        Parameters
        ----------
        node : docutil node
            Node to find replacement for.

        Returns
        -------
        nodes : node or list of node
            The replacement nodes of current node.
            Returns None if no replacement can be found.
        """
        newnode = None
        if isinstance(node, nodes.Sequential):
            newnode = self.auto_toc_tree(node)
        elif isinstance(node, nodes.literal_block):
            newnode = self.auto_code_block(node)
        elif isinstance(node, nodes.literal):
            newnode = self.auto_inline_code(node)
        return newnode

    def traverse(self, node):
        """Traverse the document tree rooted at node.

        node : docutil node
            current root node to traverse
        """
        old_level = self.current_level
        if isinstance(node, nodes.section):
            if 'level' in node:
                self.current_level = node['level']
        to_visit = []
        to_replace = []
        for c in node.children[:]:
            newnode = self.find_replace(c)
            if newnode is not None:
                to_replace.append((c, newnode))
            else:
                to_visit.append(c)

        for oldnode, newnodes in to_replace:
            node.replace(oldnode, newnodes)

        for child in to_visit:
            self.traverse(child)
        self.current_level = old_level

    def apply(self):
        """Apply the transformation by configuration."""
        source = self.document['source']

        self.reporter.info('AutoStructify: %s' % source)

        # only transform markdowns
        if not source.endswith(tuple(self.config['commonmark_suffixes'])):
            return

        self.url_resolver = self.config['url_resolver']
        assert callable(self.url_resolver)

        self.state_machine = DummyStateMachine()
        self.current_level = 0
        self.file_dir = os.path.abspath(os.path.dirname(self.document['source']))
        self.root_dir = os.path.abspath(self.document.settings.env.srcdir)
        self.traverse(self.document)
