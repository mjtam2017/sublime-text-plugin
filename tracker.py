import re
import html
import sublime
from . import utils
from . import emmet_sublime as emmet
from . import html_highlight
from . import syntax
from .emmet import ScannerException
from .emmet.token_scanner import TokenScannerException
from .emmet.abbreviation import parse as markup_parse, Abbreviation as MarkupAbbreviation
from .emmet.css_abbreviation import parse as stylesheet_parse

cache = {}
ABBR_REGION_ID = 'emmet-abbreviation'
ABBR_PREVIEW_ID = 'emmet-abbreviation-preview'

class RegionTracker:
    __slots__ = ('last_pos', 'last_length', 'region', 'forced', 'config',
                 'abbreviation', 'forced_indicator', 'offset',
                 '_has_popup_preview', '_phantom_preview')

    def __init__(self, start: int, pos: int, length: int, forced=False):
        self.last_pos = pos
        self.last_length = length
        self.forced = forced
        self.region = sublime.Region(start, pos)
        self.config = {}
        self.abbreviation = {}
        self.offset = 0
        self.forced_indicator = None
        self._has_popup_preview = False
        self._phantom_preview = None

    def shift(self, offset: int):
        "Shifts tracker location by given offset"
        self.region.a += offset
        self.region.b += offset

    def extend(self, size: int):
        "Extends or shrinks range by given size"
        self.region.b += size

    def is_valid_range(self) -> bool:
        "Check if current region is in valid state"
        return self.region.a < self.region.b or (self.region.a == self.region.b and self.forced)

    def update_abbreviation(self, view: sublime.View):
        "Updates abbreviation data from current tracker"
        abbr = view.substr(self.region)
        if self.offset:
            abbr = abbr[self.offset:]

        if not self.config:
            self.config = emmet.get_options(view, self.region.a, True)

        self.abbreviation = None

        if not abbr:
            return

        try:
            # print('parse abbreviation "%s"' % abbr)
            if self.config.get('type') == 'stylesheet':
                parsed_abbr = stylesheet_parse(abbr, self.config)
                simple = True
            else:
                parsed_abbr = markup_parse(abbr, self.config)
                simple = is_simple_markup_abbreviation(parsed_abbr)

            preview_config = self.config.copy()
            preview_config['preview'] = True
            self.abbreviation = {
                'abbr': abbr,
                'simple': simple,
                'preview': emmet.expand(parsed_abbr, preview_config)
            }

        except (ScannerException, TokenScannerException) as err:
            self.abbreviation = {
                'abbr': abbr,
                'error': {
                    'message': err.message,
                    'pos': err.pos,
                    'pointer': '%s^' % ('-' * err.pos, ) if err.pos is not None else ''
                }
            }

    def mark(self, view: sublime.View):
        "Marks tracker in given view"
        scope = emmet.get_settings('marker_scope', 'region.accent')
        mark_opt = sublime.DRAW_SOLID_UNDERLINE | sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE
        view.erase_regions(ABBR_REGION_ID)
        view.add_regions(ABBR_REGION_ID, [self.region], scope, '', mark_opt)
        if self.forced:
            phantoms = [sublime.Phantom(self.region, forced_indicator('⋮>'), sublime.LAYOUT_INLINE)]
            if not self.forced_indicator:
                self.forced_indicator = sublime.PhantomSet(view, ABBR_REGION_ID)
            self.forced_indicator.update(phantoms)

    def unmark(self, view: sublime.View):
        "Remove current tracker marker from given view"
        view.erase_regions(ABBR_REGION_ID)
        view.erase_phantoms(ABBR_REGION_ID)
        self.hide_preview(view)

    def show_preview(self, view: sublime.View, as_phantom=None):
        "Displays expanded preview of abbreviation in current tracker in given view"
        if not emmet.get_settings('abbreviation_preview', True):
            return

        content = None

        if as_phantom is None:
            # By default, display preview for CSS abbreviation as phantom to not
            # interfere with default autocomplete popup
            as_phantom = self.config and self.config['type'] == 'stylesheet'

        if not self.abbreviation:
            # No parsed abbreviation: empty region
            pass
        if 'error' in self.abbreviation:
            # Display error snippet
            err = self.abbreviation['error']
            snippet = html.escape( re.sub(r'\s+at\s\d+$', '', err['message']), False)
            content = '<div class="error pointer">%s</div><div class="error message">%s</div>' % (err['pointer'], snippet)
        elif self.forced or as_phantom or not self.abbreviation['simple']:
            snippet = self.abbreviation['preview']
            if self.config['type'] != 'stylesheet':
                if syntax.is_html(self.config['syntax']):
                    snippet = html_highlight.highlight(snippet)
                else:
                    snippet = html.escape(snippet, False)
                content = '<div class="markup-preview">%s</div>' % format_snippet(snippet)
            else:
                content = format_snippet(snippet)

        if not content:
            self.hide_preview(view)
            return

        if as_phantom:
            if not self._phantom_preview:
                self._phantom_preview = sublime.PhantomSet(view, ABBR_PREVIEW_ID)

            r = sublime.Region(self.region.end(), self.region.end())
            phantoms = [sublime.Phantom(r, preview_phantom_html(content), sublime.LAYOUT_INLINE)]
            self._phantom_preview.update(phantoms)
        else:
            self._has_popup_preview = True
            view.show_popup(
                preview_popup_html(content),
                flags=sublime.COOPERATE_WITH_AUTO_COMPLETE,
                location=self.region.begin(),
                max_width=400,
                max_height=300)

    def hide_preview(self, view: sublime.View):
        "Hides preview of current abbreviation in given view"
        if self._has_popup_preview:
            view.hide_popup()
            self._has_popup_preview = False
        if self._phantom_preview:
            view.erase_phantoms(ABBR_PREVIEW_ID)
            self._phantom_preview = None


def handle_change(view: sublime.View):
    tracker = get_tracker(view)
    if not tracker:
        return

    last_pos = tracker.last_pos
    region = tracker.region

    if last_pos < region.a or last_pos > region.b:
        # Updated content outside abbreviation: reset tracker
        stop_tracking(view)
        return

    length = view.size()
    pos = utils.get_caret(view)
    delta = length - tracker.last_length

    tracker.last_length = length
    tracker.last_pos = pos

    # print('tracker >> handle delta %d, last pos: %d, pos: %d' % (delta, last_pos, pos))

    if delta < 0:
        # Removed some content
        if last_pos == region.a:
            # Updated content at the abbreviation edge
            tracker.shift(delta)
        elif region.a < last_pos <= region.b:
            tracker.extend(delta)
    elif delta > 0 and region.a <= last_pos <= region.b:
        # Inserted content
        tracker.extend(delta)

    # Ensure range is in valid state
    if not tracker.is_valid_range():
        stop_tracking(view)
    else:
        # print('new tracker region is %s' % tracker.region)
        tracker.update_abbreviation(view)
        tracker.mark(view)
        return tracker


def handle_selection_change(view: sublime.View, caret=None):
    tracker = get_tracker(view)
    if tracker:
        if caret is None:
            caret = utils.get_caret(view)
        tracker.last_pos = caret
        return tracker


def get_tracker(view: sublime.View) -> RegionTracker:
    "Returns current abbreviation tracker for given editor, if available"
    return cache.get(view.id())


def start_tracking(view: sublime.View, start: int, pos: int, **kwargs) -> RegionTracker:
    """
    Starts abbreviation tracking for given editor
    :param start Location of abbreviation start
    :param pos Current caret position, must be greater that `start`
    """
    tracker = RegionTracker(start, pos, view.size(), kwargs.get('forced', False))
    tracker.config = kwargs.get('config')
    tracker.offset = kwargs.get('offset', 0)
    tracker.update_abbreviation(view)
    tracker.mark(view)
    cache[view.id()] = tracker
    return tracker


def stop_tracking(view: sublime.View, edit: sublime.Edit = None):
    key = view.id()
    if key in cache:
        tracker = cache.get(key)
        tracker.unmark(view)
        if tracker.forced and edit:
            # Contents of forced abbreviation must be removed
            view.erase(edit, tracker.region)
        del cache[key]


def preview_popup_html(content: str):
    style = html_highlight.styles()
    return """
    <body id="emmet-preview-popup">
        <style>
            body { line-height: 1.5rem; }
            .error { color: red }
            .error.message { font-size: 11px; line-height: 1.3rem; }
            .markup-preview { font-size: 11px; line-height: 1.3rem; }
            %s
        </style>
        <div>%s</div>
    </body>
    """ % (style, content)


def preview_phantom_html(content: str):
    return """
    <body id="emmet-preview-phantom">
        <style>
            body {
                background-color: var(--greenish);
                color: #fff;
                border-radius: 3px;
                padding: 1px 3px;
                position: relative;
            }

            .error { color: red }
        </style>
        <div class="main">%s</div>
    </body>
    """ % content


def forced_indicator(content: str):
    "Returns HTML content of forced abbreviation indicator"
    return """
        <body>
            <style>
                #emmet-forced-abbreviation {
                    background-color: var(--greenish);
                    color: #fff;
                    border-radius: 3px;
                    padding: 1px 3px;
                }
            </style>
            <div id="emmet-forced-abbreviation">%s</div>
        </body>
        """ % content


def format_snippet(text: str, class_name=None):
    class_attr = (' class="%s"' % class_name) if class_name else ''
    line_html = '<div%s style="padding-left: %dpx"><code>%s</code></div>'
    lines = [line_html % (class_attr, indent_size(line, 20), line) for line in text.splitlines()]

    return '\n'.join(lines)


def indent_size(line, width=1):
    m = re.match(r'\t+', line)
    return len(m.group(0)) * width if m else 0


def is_simple_markup_abbreviation(abbr: MarkupAbbreviation) -> bool:
    """
    Check if given parsed markup abbreviation is simple. A simple abbreviation
    may not be displayed to user as preview to reduce distraction
    """
    if len(abbr.children) == 1 and not abbr.children[0].children:
        first = abbr.children[0];
        # XXX silly check for common snippets like `!`. Should read contents
        # of expanded abbreviation instead
        return not first.name or first.name.isalpha()
    return not abbr.children
