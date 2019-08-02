import sublime
import sublime_plugin
import html
import subprocess
import json
import os
import stat
import webbrowser
import yaml

AUTOCOMPLETE_CHAR_LIMIT = 100000
MAX_RESTARTS = 10
SETTINGS_PATH = 'TabNine.sublime-settings'
PREFERENCES_PATH = 'Preferences.sublime-settings'
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))

GLOBAL_IGNORE_EVENTS = False

class TabNineCommand(sublime_plugin.TextCommand):
    def run(*args, **kwargs): #pylint: disable=W0613,E0211
        print("TabNine commands are supposed to be intercepted by TabNineListener")

class TabNineLeaderKeyCommand(TabNineCommand):
    pass
class TabNineReverseLeaderKeyCommand(TabNineCommand):
    pass

class TabNineSubstituteCommand(sublime_plugin.TextCommand):
    def run(
        self, edit, *,
        region_begin, region_end, substitution, new_cursor_pos,
        prefix, old_prefix, documentation, expected_prefix
    ):
        normalize_offset = -self.view.sel()[0].begin()
        def normalize(x, sel):
            if isinstance(x, sublime.Region):
                return sublime.Region(normalize(x.begin(), sel), normalize(x.end(), sel))
            else:
                return normalize_offset + x + sel.begin() 
        observed_prefixes = [
            self.view.substr(sublime.Region(normalize(region_begin, sel), sel.begin()))
            for sel in self.view.sel()
        ]
        if old_prefix is not None:
            for i in range(len(self.view.sel())):
                sel = self.view.sel()[i]
                t_region_end = normalize(region_end, sel)
                self.view.sel().subtract(sel)
                self.view.insert(edit, t_region_end, old_prefix)
                self.view.sel().add(t_region_end)
        normalize_offset = -self.view.sel()[0].begin()
        region_end += len(prefix)
        region = sublime.Region(region_begin, region_end)
        for i in range(len(self.view.sel())):
            sel = self.view.sel()[i]
            t_region = normalize(region, sel)
            observed_prefix = observed_prefixes[i]
            if observed_prefix != expected_prefix:
                new_begin = self.view.word(sel).begin()
                print(
                    'TabNine expected prefix "{}" but found prefix "{}", falling back to substituting from word beginning: "{}"'
                        .format(expected_prefix, observed_prefix, self.view.substr(sublime.Region(new_begin, sel.begin())))
                )
                t_region = sublime.Region(new_begin, t_region.end())
            self.view.sel().subtract(sel)
            self.view.erase(edit, t_region)
            self.view.insert(edit, t_region.begin(), substitution)
            self.view.sel().add(t_region.begin() + new_cursor_pos)
        if documentation is None:
            self.view.hide_popup()
        else:
            if isinstance(documentation, dict) and 'kind' in documentation and documentation['kind'] == 'markdown' and 'value' in documentation:
                my_show_popup(self.view, documentation['value'], region_begin, markdown=True)
            else:
                my_show_popup(self.view, str(documentation), region_begin, markdown=False)

class TabNineListener(sublime_plugin.EventListener):
    def __init__(self):
        self.before = ""
        self.after = ""
        self.region_includes_beginning = False
        self.region_includes_end = False
        self.before_begin_location = 0
        self.autocompleting = False
        self.choices = []
        self.substitute_interval = 0, 0
        self.actions_since_completion = 1
        self.install_directory = os.path.dirname(os.path.realpath(__file__))
        self.tabnine_proc = None
        self.num_restarts = 0
        self.old_prefix = None
        self.popup_is_ours = False
        self.seen_changes = False
        self.syntax_ext_map = {}

        self.tab_index = 0
        self.old_prefix = None
        self.expected_prefix = ""

        def on_change():
            self.num_restarts = 0
            self.restart_tabnine_proc()
        sublime.load_settings(SETTINGS_PATH).add_on_change('TabNine', on_change)
        sublime.load_settings(PREFERENCES_PATH).set('auto_complete', False)
        sublime.save_settings(PREFERENCES_PATH)

    def restart_tabnine_proc(self):
        if self.tabnine_proc is not None:
            try:
                self.tabnine_proc.terminate()
            except Exception: #pylint: disable=W0703
                pass
        binary_dir = os.path.join(self.install_directory, "binaries")
        settings = sublime.load_settings(SETTINGS_PATH)
        tabnine_path = settings.get("custom_binary_path")
        if tabnine_path is None:
            tabnine_path = get_tabnine_path(binary_dir)
        args = [tabnine_path, "--client", "sublime"]
        log_file_path = settings.get("log_file_path")
        if log_file_path is not None:
            args += ["--log-file-path", log_file_path]
        extra_args = settings.get("extra_args")
        if extra_args is not None:
            args += extra_args
        self.tabnine_proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            startupinfo=get_startup_info(sublime.platform()))

    def request(self, req):
        if self.tabnine_proc is None:
            self.restart_tabnine_proc()
        if self.tabnine_proc.poll():
            print("TabNine subprocess is dead")
            if self.num_restarts < MAX_RESTARTS:
                print("Restarting it...")
                self.num_restarts += 1
                self.restart_tabnine_proc()
            else:
                return None
        req = {
            "version": "1.0.0",
            "request": req
        }
        req = json.dumps(req)
        req += '\n'
        try:
            self.tabnine_proc.stdin.write(bytes(req, "UTF-8"))
            self.tabnine_proc.stdin.flush()
            result = self.tabnine_proc.stdout.readline()
            result = str(result, "UTF-8")
            return json.loads(result)
        except (IOError, OSError, UnicodeDecodeError, ValueError) as e:
            print("Exception while interacting with TabNine subprocess:", e) 
            if self.num_restarts < MAX_RESTARTS:
                self.num_restarts += 1
                self.restart_tabnine_proc()

    def get_before(self, view, char_limit):
        loc = view.sel()[0].begin()
        begin = max(0, loc - char_limit)
        return view.substr(sublime.Region(begin, loc)), begin == 0, loc
    def get_after(self, view, char_limit):
        loc = view.sel()[0].end()
        end = min(view.size(), loc + char_limit)
        return view.substr(sublime.Region(loc, end)), end == view.size()

    def on_modified(self, view):
        self.seen_changes = True
        self.on_any_event(view)
    def on_selection_modified(self, view):
        self.on_any_event(view)
    def on_activated(self, view):
        self.on_any_event(view)

    def on_activated_async(self, view):
        file_name = view.file_name()
        if file_name is not None:
            request = {
                "Prefetch": {
                    "filename": file_name
                }
            }
            self.request(request)

    def on_any_event(self, view):
        if view.window() is None:
            return
        view = view.window().active_view()
        if GLOBAL_IGNORE_EVENTS:
            return
        (
            new_before,
            self.region_includes_beginning,
            self.before_begin_location,
        ) = self.get_before(view, AUTOCOMPLETE_CHAR_LIMIT)
        new_after, self.region_includes_end = self.get_after(view, AUTOCOMPLETE_CHAR_LIMIT)
        if new_before == self.before and new_after == self.after:
            return
        self.autocompleting = self.should_autocomplete(
            view,
            old_before=self.before,
            old_after=self.after,
            new_before=new_before,
            new_after=new_after)
        self.before = new_before
        self.after = new_after
        self.actions_since_completion += 1
        if self.autocompleting:
            pass # on_selection_modified_async will show the popup
        else:
            if self.popup_is_ours:
                view.hide_popup()
                self.popup_is_ours = False
            if self.actions_since_completion >= 2:
                self.choices = []

    def should_autocomplete(self, view, *, old_before, old_after, new_before, new_after):
        return (self.actions_since_completion >= 1
            and len(view.sel()) <= 100
            and all(sel.begin() == sel.end() for sel in view.sel())
            and self.all_same_prefix(view, [sel.begin() for sel in view.sel()])
            and self.all_same_suffix(view, [sel.begin() for sel in view.sel()])
            and new_before != ""
            and (new_after[:100] != old_after[1:101] or new_after == "" or (len(view.sel()) >= 2 and self.seen_changes))
            and old_before[-100:] == new_before[-101:-1])

    def all_same_prefix(self, view, positions):
        return self.all_same(view, positions, -1, -1)
    def all_same_suffix(self, view, positions):
        return self.all_same(view, positions, 0, 1)

    def all_same(self, view, positions, start, step):
        if len(positions) <= 1:
            return True
        # We should ask TabNine for the identifier regex but this is simpler for now
        def alnum_char_at(i):
            if i >= 0:
                s = view.substr(sublime.Region(i, i+1))
                if s.isalnum():
                    return s
            return None
        offset = start
        while True:
            next_chars = {alnum_char_at(pos + offset) for pos in positions}
            if len(next_chars) != 1:
                return False
            if next(iter(next_chars)) is None:
                return True
            if offset <= -30:
                return True
            offset += step

    def get_settings(self):
        return sublime.load_settings(SETTINGS_PATH)

    def get_dummy_file(self, view):
        syntax_file = view.settings().get('syntax')
        if syntax_file not in self.syntax_ext_map:
            self.syntax_ext_map[syntax_file] = None
            try:
                syntax_yaml = yaml.safe_load(sublime.load_resource(syntax_file))
                extension = syntax_yaml['file_extensions'][0]
                dummy_file = os.path.join(CONFIG_DIR, 'fake_project', 'foo.' + extension)
                self.syntax_ext_map[syntax_file] = dummy_file
            except Exception as e:
                print('Failed to get extension for syntax file %s:' % syntax_file, e)
        return self.syntax_ext_map[syntax_file]

    def max_num_results(self):
        return self.get_settings().get("max_num_results")

    def on_selection_modified_async(self, view):
        if view.window() is None:
            return
        view = view.window().active_view()
        if not self.autocompleting:
            return
        max_num_results = self.max_num_results()
        request = {
            "Autocomplete": {
                "before": self.before,
                "after": self.after,
                "filename": view.file_name() or self.get_dummy_file(view),
                "region_includes_beginning": self.region_includes_beginning,
                "region_includes_end": self.region_includes_end,
                "max_num_results": max_num_results,
            }
        }
        response = self.request(request)
        if response is None or not self.autocompleting:
            return
        self.tab_index = None
        self.old_prefix = None
        self.expected_prefix = response["old_prefix"]
        self.choices = response["results"]
        max_choices = 9
        if max_num_results is not None:
            max_choices = min(max_choices, max_num_results)
        self.choices = self.choices[:max_choices]
        substitute_begin = self.before_begin_location - len(self.expected_prefix)
        self.substitute_interval = (substitute_begin, self.before_begin_location)
        to_show = [choice["new_prefix"] for choice in self.choices]
        max_len = max([len(x) for x in to_show] or [0])
        show_detail = self.get_settings().get("detail")
        for i in range(len(to_show)):
            padding = max_len - len(to_show[i])
            if i <= 1:
                annotation = "Tab" + "+Tab" * i
            elif i <= 8:
                annotation = "Tab+" + str(i+1)
            else:
                annotation = ""
            detail_padding = 3 + 4 * 1 - len(annotation) + 2
            annotation = "<i>" + annotation + "</i>"
            choice = self.choices[i]
            if show_detail and 'detail' in choice and isinstance(choice['detail'], str):
                annotation += escape(" " * detail_padding + choice['detail'].replace('\n', ' '))
            with_padding = escape(to_show[i] + " " * padding) + "&nbsp;" * 2
            to_show[i] = with_padding + annotation
        if "user_message" in response:
            for line in response["user_message"]:
                to_show.append("""<span style="font-size: 10;">""" + escape(line) + "</span>")
        to_show = "<br>".join(to_show)

        if self.choices == []:
            if self.popup_is_ours:
                view.hide_popup()
        else:
            my_show_popup(view, to_show, substitute_begin)
            self.popup_is_ours = True
            self.seen_changes = False

    def insert_completion(self, view, choice_index): #pylint: disable=W0613
        self.tab_index = choice_index
        a, b = self.substitute_interval
        choice = self.choices[choice_index]
        new_prefix = choice["new_prefix"]
        prefix = choice["old_suffix"] # The naming here is very bad
        new_suffix = choice["new_suffix"]
        substitution = new_prefix + new_suffix
        self.substitute_interval = a, (a + len(substitution))
        self.actions_since_completion = 0
        if len(self.choices) == 1:
            self.choices = []
        if self.get_settings().get("documentation"):
            documentation = get_additional_detail(choice)
        else:
            documentation = None
        new_args = {
            "region_begin": a,
            "region_end": b,
            "substitution": substitution,
            "new_cursor_pos": len(new_prefix),
            "prefix": prefix,
            "old_prefix": self.old_prefix,
            "documentation": documentation,
            "expected_prefix": self.expected_prefix,
        }
        self.expected_prefix = new_prefix
        if documentation is not None:
            self.popup_is_ours = False
        self.old_prefix = prefix
        return "tab_nine_substitute", new_args

    def on_text_command(self, view, command_name, args):
        if command_name == "tab_nine" and "num" in args:
            num = args["num"]
            choice_index = num - 1
            if choice_index < 0 or choice_index >= len(self.choices):
                return None
            result = self.insert_completion(view, choice_index)
            self.choices = []
            return result
        if command_name in ["insert_best_completion", "tab_nine_leader_key"] and len(self.choices) >= 1:
            index = 0 if self.tab_index is None or self.tab_index == len(self.choices) - 1 else self.tab_index + 1
            return self.insert_completion(view, index)
        if command_name == "tab_nine_reverse_leader_key" and len(self.choices) >= 1:
            index = len(self.choices) - 1 if self.tab_index is None or self.tab_index == 0 else self.tab_index - 1
            return self.insert_completion(view, index)

    def on_query_context(self, view, key, operator, operand, match_all): #pylint: disable=W0613
        if key == "tab_nine_choice_available":
            assert operator == sublime.OP_EQUAL
            return (not self.popup_is_ours) and 1 <= operand <= len(self.choices)
        if key == "tab_nine_leader_key_available":
            assert operator == sublime.OP_EQUAL
            return (self.choices != [] and view.is_popup_visible()) == operand
        if key == "tab_nine_reverse_leader_key_available":
            assert operator == sublime.OP_EQUAL
            return (self.choices != []) == operand

def get_startup_info(platform):
    if platform == "windows":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return si
    else:
        return None

def escape(s):
    s = html.escape(s, quote=False)
    s = s.replace(" ", "&nbsp;")
    urls = [
        ('https://tabnine.com/semantic', None, 'tabnine.com/semantic'),
        ('tabnine.com/semantic', 'https://tabnine.com/semantic', 'tabnine.com/semantic'),
        ('tabnine.com', 'https://tabnine.com', 'tabnine.com'),
    ]
    for url, navigate_to, display in urls:
        if url in s:
            if navigate_to is None:
                navigate_to = url
            s = s.replace(html.escape(url), '<a href="{}">{}</a>'.format(url, display))
            break
    return s

def get_additional_detail(choice):
    s = None
    if 'documentation' in choice:
        s = choice['documentation']
    return s

def format_documentation(documentation):
    if isinstance(documentation, str):
        return escape(documentation)
    elif isinstance(documentation, dict) and 'kind' in documentation and documentation['kind'] == 'markdown' and 'value' in documentation:
        return escape(documentation['value'])
    else:
        return escape(str(documentation))

def parse_semver(s):
    try:
        return [int(x) for x in s.split('.')]
    except ValueError:
        return []

assert parse_semver("0.01.10") == [0, 1, 10]
assert parse_semver("hello") == []
assert parse_semver("hello") < parse_semver("0.9.0") < parse_semver("1.0.0")

def my_show_popup(view, content, location, markdown=None):
    global GLOBAL_IGNORE_EVENTS
    GLOBAL_IGNORE_EVENTS = True
    if markdown is None:
        view.show_popup(
            content,
            sublime.COOPERATE_WITH_AUTO_COMPLETE,
            location=location,
            max_width=600,
            max_height=400,
            on_navigate=webbrowser.open,
        )
    else:
        content = escape(content)
        view.show_popup(
            content,
            sublime.COOPERATE_WITH_AUTO_COMPLETE,
            location=location,
            max_width=800,
            max_height=400,
            on_navigate=webbrowser.open,
        )
    GLOBAL_IGNORE_EVENTS = False

def get_tabnine_path(binary_dir):
    def join_path(*args):
        return os.path.join(binary_dir, *args)
    translation = {
        ("linux",   "x32"): "i686-unknown-linux-gnu/TabNine",
        ("linux",   "x64"): "x86_64-unknown-linux-gnu/TabNine",
        ("osx",     "x32"): "i686-apple-darwin/TabNine",
        ("osx",     "x64"): "x86_64-apple-darwin/TabNine",
        ("windows", "x32"): "i686-pc-windows-gnu/TabNine.exe",
        ("windows", "x64"): "x86_64-pc-windows-gnu/TabNine.exe",
    }
    versions = os.listdir(binary_dir)
    versions.sort(key=parse_semver, reverse=True)
    for version in versions:
        key = sublime.platform(), sublime.arch()
        path = join_path(version, translation[key])
        if os.path.isfile(path):
            add_execute_permission(path)
            print("TabNine: starting version", version)
            return path

def add_execute_permission(path):
    st = os.stat(path)
    new_mode = st.st_mode | stat.S_IEXEC
    if new_mode != st.st_mode:
        os.chmod(path, new_mode)
