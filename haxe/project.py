import json
import sublime
import os
import re

import haxe.build as hxbuild
import haxe.panel as hxpanel
import haxe.hxtools as hxsrctools
import haxe.types as hxtypes
import haxe.settings as hxsettings
import haxe.tools.path as path_tools
import haxe.compiler.server as hxserver

from haxe.execute import run_cmd
from haxe.log import log
from haxe.tools.cache import Cache

classpath_line = re.compile("Classpath : (.*)")

haxe_version = re.compile("haxe_([0-9]{3})",re.M)


class Project:
    def __init__(self, id, file, win_id, server_port):
        from haxe.complete import CompletionContext
        self.completion_context = CompletionContext()
        self.current_build = None
        self.selecting_build = False
        self.builds = []
        self.win_id = win_id
        
        self.server = hxserver.Server(server_port)

        
        self.project_file = file
        self.project_id = id
        if (self.project_file != None):
            self.project_path = os.path.normpath(os.path.dirname(self.project_file))
        else:
            self.project_path = None

        self.update_compiler_info()

    def project_dir (self, default):
        p = default
        if self.project_path != None:
            p = self.project_path
            
        return p

    def start_server(self, view):
        haxepath = hxsettings.haxe_exec(view)
                 
        merged_env = os.environ.copy()
        
        if view is not None :
            user_env = view.settings().get('build_env')
            if user_env:
                merged_env.update(user_env)
            libPath = hxsettings.haxe_library_path()
            if libPath != None :
                merged_env["HAXE_LIBRARY_PATH"] = libPath
    
        cwd = self.project_dir(".")
        log( "server cwd: " + cwd)
        if "HAXE_LIBRARY_PATH" in merged_env:
            log( "server env: " + merged_env["HAXE_LIBRARY_PATH"])

        self.server.start(haxepath, cwd, merged_env)
        

    def update_compiler_info (self):
        classes, packs, ver, std_paths = collect_compiler_info(self.project_path)

        self.serverMode = int(ver.group(1)) >= 209

        self.std_paths = std_paths
        self.std_packages = packs
        self.std_classes = ["Void","String", "Float", "Int", "UInt", "Bool", "Dynamic", "Iterator", "Iterable", "ArrayAccess"]
        self.std_classes.extend(classes)

    def is_server_mode (self):
        return self.serverMode and hxsettings.get_bool('haxe-use-server-mode', True)

    def generate_build(self, view) :

        fn = view.file_name()

        if self.current_build is not None and fn == self.current_build.hxml and view.size() == 0 :  
            e = view.begin_edit()
            hxml_src = self.current_build.make_hxml()
            view.insert(e,0,hxml_src)
            view.end_edit(e)

    def select_build( self, view ) :
        scopes = view.scope_name(view.sel()[0].end()).split()
        
        if 'source.hxml' in scopes:
            view.run_command("save")

        self.extract_build_args( view , True )


    def extract_build_args( self, view , force_panel = False ) :
    
        self.builds = []

        fn = view.file_name()


        settings = view.settings()

        #log("filename: " + fn)

        folder = os.path.dirname(fn)
        

        folders = view.window().folders()
        
        for f in folders:
            self.builds.extend(hxbuild.find_hxmls(f))
            self.builds.extend(hxbuild.find_nmmls(f))
                

        
        log( "num builds:" + str(len(self.builds)))

        # settings.set("haxe-complete-folder", folder)
        

        if len(self.builds) == 1:
            if force_panel : 
                sublime.status_message("There is only one build")

            # will open the build file
            #if force_panel :
            #   b = builds[0]
            #   f = b.hxml
            #   v = view.window().open_file(f,sublime.TRANSIENT) 

            self.set_current_build( view , int(0), force_panel )

        elif len(self.builds) == 0 and force_panel :
            sublime.status_message("No hxml or nmml file found")

            f = os.path.join(folder,"build.hxml")

            self.current_build = None
            self.get_build(view)
            self.current_build.hxml = f

            #for whatever reason generate_build doesn't work without transient
            view.window().open_file(f,sublime.TRANSIENT)

            self.set_current_build( view , int(0), force_panel )

        elif len(self.builds) > 1 and force_panel :
            buildsView = []
            for b in self.builds :
                #for a in b.args :
                #   v.append( " ".join(a) )
                buildsView.append( [b.to_string(), os.path.basename( b.hxml ) ] )

            self.selecting_build = True
            sublime.status_message("Please select your build")
            view.window().show_quick_panel( buildsView , lambda i : self.set_current_build(view, int(i), force_panel) , sublime.MONOSPACE_FONT )

        elif settings.has("haxe-build-id"):
            self.set_current_build( view , int(settings.get("haxe-build-id")), force_panel )
        
        else:
            self.set_current_build( view , int(0), force_panel )

    def set_current_build( self, view , id , force_panel ) :
        
        log( "set_current_build")
        if id < 0 or id >= len(self.builds) :
            id = 0
        
        view.settings().set( "haxe-build-id" , id ) 

        if len(self.builds) > 0 :
            self.current_build = self.builds[id]
            #log( "set_current_build - 2")
            hxpanel.default_panel().status( "haxe-build" , self.current_build.to_string() )
        else:
            hxpanel.default_panel().status( "haxe-build" , "No build" )
            
        self.selecting_build = False

        if force_panel and self.current_build is not None: # choose NME target
            if self.current_build.nmml is not None:
                sublime.status_message("Please select a NME target")
                nme_targets = []
                for t in hxbuild.HaxeBuild.nme_targets :
                    nme_targets.append( t[0] )

                view.window().show_quick_panel(nme_targets, lambda i : select_nme_target(self.current_build, i, view))

    def run_build( self, view ) :
        
        haxeExec = hxsettings.haxe_exec(view)
        self.extract_build_args(view)
        build = self.get_build(view)

        out, err = build.run(haxeExec, self.serverMode, view, self)
        log( out)
        log( err)
        log( "run_build_complete")
        hxpanel.default_panel().writeln(err)
        view.set_status( "haxe-status" , "build finished" )

    def clear_build( self ) :
        self.current_build = None
        self.completion_context.clear_completion()



    def __del__(self) :
        log( "kill server")
        self.server.stop()


    def get_build( self, view ) :
        
        if self.current_build is None and view.score_selector(0,"source.haxe.2") > 0 :

            fn = view.file_name()

            src_dir = os.path.dirname( fn )

            src = view.substr(sublime.Region(0, view.size()))
        
            build = hxbuild.HaxeBuild()
            build.target = "js"

            folder = os.path.dirname(fn)
            folders = view.window().folders()
            for f in folders:
                if f in fn :
                    folder = f

            pack = []
            for ps in hxsrctools.package_line.findall( src ) :
                if ps == "":
                    continue
                    
                pack = ps.split(".")
                for p in reversed(pack) : 
                    spl = os.path.split( src_dir )
                    if( spl[1] == p ) :
                        src_dir = spl[0]

            cl = os.path.basename(fn)
            cl = cl.encode('ascii','ignore')
            cl = cl[0:cl.rfind(".")]

            main = pack[0:]
            main.append( cl )
            build.main = ".".join( main )

            build.output = os.path.join(folder,build.main.lower() + ".js")

            log( "add cp: " + src_dir)

            build.args.append( ("-cp" , src_dir) )
            #build.args.append( ("-main" , build.main ) )

            build.args.append( ("-js" , build.output ) )
            #build.args.append( ("--no-output" , "-v" ) )

            build.hxml = os.path.join( src_dir , "build.hxml")
            
            #build.hxml = os.path.join( src_dir , "build.hxml")
            self.current_build = build
            
        return self.current_build   



# last time the sublime session file was updated
_last_modification_time = None
# used for caching the path of current project file
_last_project = None
# hash to store all active projects, files without project file use the "global" context




def run_nme( view, build ) :

    cmd = [ hxsettings.haxelib_exec(), "run", "nme", hxbuild.HaxeBuild.nme_target[2], os.path.basename(build.nmml) ]
    target = hxbuild.HaxeBuild.nme_target[1].split(" ")
    cmd.extend(target)
    cmd.append("-debug")

    view.window().run_command("exec", {
        "cmd": cmd,
        "working_dir": os.path.dirname(build.nmml),
        "file_regex": "^([^:]*):([0-9]+): characters [0-9]+-([0-9]+) :.*$"
    })
    return ("" , [], "" )



def get_compiler_info_env (project_path):
    lib_path = hxsettings.haxe_library_path();
    env = os.environ.copy()
    if lib_path != None :
        abs_lib_path = path_tools.join_norm(project_path, lib_path)
        env["HAXE_LIBRARY_PATH"] = abs_lib_path
        log("export HAXE_LIBRARY_PATH=" + abs_lib_path)
    return env


def collect_compiler_info (project_path):
    log("collect compiler info")
    haxe_exec = hxsettings.haxe_exec()
    
    env = get_compiler_info_env(project_path)

    if haxe_exec != "haxe":
        if project_path != None:
            haxe_exec = path_tools.join_norm(project_path, haxe_exec)
    
    out, err = run_cmd( [haxe_exec, "-main", "Nothing", "-v", "--no-output"], env=env )
    log( out )
    m = classpath_line.match(out)
    
    classes = []
    packs = []
    std_paths = []

    if m is not None :
        std_paths = set(m.group(1).split(";")) - set([".","./"])
    
    for p in std_paths : 
        if len(p) > 1 and os.path.exists(p) and os.path.isdir(p):
            classes, packs = hxtypes.extract_types( p, [], [] )
            

    ver = re.search( haxe_version , out )
    log("collected classes: " + str(len(classes)))
    return (classes, packs, ver, std_paths)

def _get_project_file(win_id = None):
    global _last_project
    global _last_modification_time

    log( "try getting project file")

    if win_id == None:
        win_id = sublime.active_window().id()

    project = None
    reg_session = os.path.join(sublime.packages_path(), "..", "Settings", "Session.sublime_session")
    auto_save = os.path.join(sublime.packages_path(), "..", "Settings", "Auto Save Session.sublime_session")
    session = auto_save if os.path.exists(auto_save) else reg_session



    if not os.path.exists(session) or win_id == None:
        return project


    mtime = os.path.getmtime(session)

    if (_last_modification_time is not None 
        and mtime == _last_modification_time
        and _last_project != None):
        _last_modification_time = mtime
        log( "cached project id")
        return _last_project
    else:
        _last_modification_time = mtime
    try:
        with open(session, 'r') as f:
            # Tabs in strings messes things up for some reason
            j = json.JSONDecoder(strict=False).decode(f.read())
            for w in j['windows']:
                if w['window_id'] == win_id:
                    if "workspace_name" in w:
                        if sublime.platform() == "windows":
                            # Account for windows specific formatting
                            project = os.path.normpath(w["workspace_name"].lstrip("/").replace("/", ":/", 1))
                        else:
                            project = w["workspace_name"]
                        break
    except:
        pass

    # Throw out empty project names
    if project == None or re.match(".*\\.sublime-project", project) == None or not os.path.exists(project):
        project = None

    _last_project = project
    return project


def select_nme_target( build, i, view ):
    target = hxbuild.HaxeBuild.nme_targets[i]
    if build.nmml is not None:
        hxbuild.HaxeBuild.nme_target = target
        view.set_status( "haxe-build" , build.to_string() )
        hxpanel.default_panel().status( "haxe-build" , build.to_string() )



_projects = Cache()
_next_server_port = [6000]
def current_project(view = None):


    log("next server port: " + str(_next_server_port[0]))

    win_ids = [w.id() for w in sublime.windows()]

    remove = []
    for p in _projects.data.iterkeys():
        proj = _projects.get_or_default(p, None)
        if proj != None and proj.win_id not in win_ids:
            remove.append(p)
            # project should be closed
    
    log(remove)
    for pid in remove:
        log(pid)
        project = _projects.data[pid]
        log("delete project from memory")
        del _projects.data[pid]
        del project


    

    file = _get_project_file()
    
    if (view != None):
        win = view.window();
        if win == None:
            win = sublime.active_window()
    else:
        win = sublime.active_window()
    if (file == None):
        id = "global" + str(win.id())
    else:
        id = file
    log("project id:" + id)
    def create ():
                
        p = Project(id, file, win.id(), _next_server_port[0])
        _next_server_port[0] = _next_server_port[0] + 1
        return p
    res = _projects.get_or_insert(id, create )
    
    return res