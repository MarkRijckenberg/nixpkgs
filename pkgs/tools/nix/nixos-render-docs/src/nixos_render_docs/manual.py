import argparse
import json

from abc import abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast, Generic, NamedTuple, Optional, Union
from xml.sax.saxutils import escape, quoteattr

import markdown_it
from markdown_it.token import Token

from . import md, options
from .docbook import DocBookRenderer, Heading
from .md import Converter

class BaseConverter(Converter[md.TR], Generic[md.TR]):
    _base_paths: list[Path]

    def convert(self, file: Path) -> str:
        self._base_paths = [ file ]
        try:
            with open(file, 'r') as f:
                return self._render(f.read())
        except Exception as e:
            raise RuntimeError(f"failed to render manual {file}") from e

    def _parse(self, src: str) -> list[Token]:
        tokens = super()._parse(src)
        for token in tokens:
            if token.type != "fence" or not token.info.startswith("{=include=} "):
                continue
            typ = token.info[12:].strip()
            if typ == 'options':
                token.type = 'included_options'
                self._parse_options(token)
            elif typ in [ 'sections', 'chapters', 'preface', 'parts', 'appendix' ]:
                token.type = 'included_' + typ
                self._parse_included_blocks(token)
            else:
                raise RuntimeError(f"unsupported structural include type '{typ}'")
        return tokens

    def _parse_included_blocks(self, token: Token) -> None:
        assert token.map
        included = token.meta['included'] = []
        for (lnum, line) in enumerate(token.content.splitlines(), token.map[0] + 2):
            line = line.strip()
            path = self._base_paths[-1].parent / line
            if path in self._base_paths:
                raise RuntimeError(f"circular include found in line {lnum}")
            try:
                self._base_paths.append(path)
                with open(path, 'r') as f:
                    tokens = self._parse(f.read())
                    included.append((tokens, path))
                self._base_paths.pop()
            except Exception as e:
                raise RuntimeError(f"processing included file {path} from line {lnum}") from e

    def _parse_options(self, token: Token) -> None:
        assert token.map

        items = {}
        for (lnum, line) in enumerate(token.content.splitlines(), token.map[0] + 2):
            if len(args := line.split(":", 1)) != 2:
                raise RuntimeError(f"options directive with no argument in line {lnum}")
            (k, v) = (args[0].strip(), args[1].strip())
            if k in items:
                raise RuntimeError(f"duplicate options directive {k} in line {lnum}")
            items[k] = v
        try:
            id_prefix = items.pop('id-prefix')
            varlist_id = items.pop('list-id')
            source = items.pop('source')
        except KeyError as e:
            raise RuntimeError(f"options directive {e} missing in block at line {token.map[0] + 1}")
        if items.keys():
            raise RuntimeError(
                f"unsupported options directives in block at line {token.map[0] + 1}",
                " ".join(items.keys()))

        try:
            with open(self._base_paths[-1].parent / source, 'r') as f:
                token.meta['id-prefix'] = id_prefix
                token.meta['list-id'] = varlist_id
                token.meta['source'] = json.load(f)
        except Exception as e:
            raise RuntimeError(f"processing options block in line {token.map[0] + 1}") from e

class ManualDocBookRenderer(DocBookRenderer):
    _toplevel_tag: str
    _revision: str

    def __init__(self, toplevel_tag: str, revision: str, manpage_urls: Mapping[str, str]):
        super().__init__(manpage_urls)
        self._toplevel_tag = toplevel_tag
        self._revision = revision
        self.rules |= {
            'included_sections': lambda *args: self._included_thing("section", *args),
            'included_chapters': lambda *args: self._included_thing("chapter", *args),
            'included_preface': lambda *args: self._included_thing("preface", *args),
            'included_parts': lambda *args: self._included_thing("part", *args),
            'included_appendix': lambda *args: self._included_thing("appendix", *args),
            'included_options': self.included_options,
        }

    def render(self, tokens: Sequence[Token]) -> str:
        wanted = { 'h1': 'title' }
        wanted |= { 'h2': 'subtitle' } if self._toplevel_tag == 'book' else {}
        for (i, (tag, kind)) in enumerate(wanted.items()):
            if len(tokens) < 3 * (i + 1):
                raise RuntimeError(f"missing {kind} ({tag}) heading")
            token = tokens[3 * i]
            if token.type != 'heading_open' or token.tag != tag:
                assert token.map
                raise RuntimeError(f"expected {kind} ({tag}) heading in line {token.map[0] + 1}", token)
        for t in tokens[3 * len(wanted):]:
            if t.type != 'heading_open' or (info := wanted.get(t.tag)) is None:
                continue
            assert t.map
            raise RuntimeError(
                f"only one {info[0]} heading ({t.markup} [text...]) allowed per "
                f"{self._toplevel_tag}, but found a second in lines [{t.map[0] + 1}..{t.map[1]}]. "
                "please remove all such headings except the first or demote the subsequent headings.",
                t)

        # books get special handling because they have *two* title tags. doing this with
        # generic code is more complicated than it's worth. the checks above have verified
        # that both titles actually exist.
        if self._toplevel_tag == 'book':
            assert tokens[1].children
            assert tokens[4].children
            if (maybe_id := cast(str, tokens[0].attrs.get('id', ""))):
                maybe_id = "xml:id=" + quoteattr(maybe_id)
            return (f'<book xmlns="http://docbook.org/ns/docbook"'
                    f'      xmlns:xlink="http://www.w3.org/1999/xlink"'
                    f'      {maybe_id} version="5.0">'
                    f'  <title>{self.renderInline(tokens[1].children)}</title>'
                    f'  <subtitle>{self.renderInline(tokens[4].children)}</subtitle>'
                    f'  {super().render(tokens[6:])}'
                    f'</book>')

        return super().render(tokens)

    def _heading_tag(self, token: Token, tokens: Sequence[Token], i: int) -> tuple[str, dict[str, str]]:
        (tag, attrs) = super()._heading_tag(token, tokens, i)
        # render() has already verified that we don't have supernumerary headings and since the
        # book tag is handled specially we can leave the check this simple
        if token.tag != 'h1':
            return (tag, attrs)
        return (self._toplevel_tag, attrs | {
            'xmlns': "http://docbook.org/ns/docbook",
            'xmlns:xlink': "http://www.w3.org/1999/xlink",
        })

    def _included_thing(self, tag: str, token: Token, tokens: Sequence[Token], i: int) -> str:
        result = []
        # close existing partintro. the generic render doesn't really need this because
        # it doesn't have a concept of structure in the way the manual does.
        if self._headings and self._headings[-1] == Heading('part', 1):
            result.append("</partintro>")
            self._headings[-1] = self._headings[-1]._replace(partintro_closed=True)
        # must nest properly for structural includes. this requires saving at least
        # the headings stack, but creating new renderers is cheap and much easier.
        r = ManualDocBookRenderer(tag, self._revision, self._manpage_urls)
        for (included, path) in token.meta['included']:
            try:
                result.append(r.render(included))
            except Exception as e:
                raise RuntimeError(f"rendering {path}") from e
        return "".join(result)
    def included_options(self, token: Token, tokens: Sequence[Token], i: int) -> str:
        conv = options.DocBookConverter(self._manpage_urls, self._revision, False, 'fragment',
                                        token.meta['list-id'], token.meta['id-prefix'])
        conv.add_options(token.meta['source'])
        return conv.finalize(fragment=True)

    # TODO minimize docbook diffs with existing conversions. remove soon.
    def paragraph_open(self, token: Token, tokens: Sequence[Token], i: int) -> str:
        return super().paragraph_open(token, tokens, i) + "\n "
    def paragraph_close(self, token: Token, tokens: Sequence[Token], i: int) -> str:
        return "\n" + super().paragraph_close(token, tokens, i)
    def code_block(self, token: Token, tokens: Sequence[Token], i: int) -> str:
        return f"<programlisting>\n{escape(token.content)}</programlisting>"
    def fence(self, token: Token, tokens: Sequence[Token], i: int) -> str:
        info = f" language={quoteattr(token.info)}" if token.info != "" else ""
        return f"<programlisting{info}>\n{escape(token.content)}</programlisting>"

class DocBookConverter(BaseConverter[ManualDocBookRenderer]):
    def __init__(self, manpage_urls: Mapping[str, str], revision: str):
        super().__init__()
        self._renderer = ManualDocBookRenderer('book', revision, manpage_urls)



def _build_cli_db(p: argparse.ArgumentParser) -> None:
    p.add_argument('--manpage-urls', required=True)
    p.add_argument('--revision', required=True)
    p.add_argument('infile', type=Path)
    p.add_argument('outfile', type=Path)

def _run_cli_db(args: argparse.Namespace) -> None:
    with open(args.manpage_urls, 'r') as manpage_urls:
        md = DocBookConverter(json.load(manpage_urls), args.revision)
        converted = md.convert(args.infile)
        args.outfile.write_text(converted)

def build_cli(p: argparse.ArgumentParser) -> None:
    formats = p.add_subparsers(dest='format', required=True)
    _build_cli_db(formats.add_parser('docbook'))

def run_cli(args: argparse.Namespace) -> None:
    if args.format == 'docbook':
        _run_cli_db(args)
    else:
        raise RuntimeError('format not hooked up', args)
