// The launchd stub for the mouse-wheel daemon. Build it into the bundle (the binary is
// not tracked — it must be signed on the machine that grants it Accessibility):
//
//   mkdir -p launchd/GigakuSound.app/Contents/MacOS
//   clang -framework Foundation -x objective-c \
//         -o launchd/GigakuSound.app/Contents/MacOS/GigakuSound launchd/GigakuSound.m
//   codesign --force --deep --sign - launchd/GigakuSound.app
//
#import <Foundation/Foundation.h>
#include <mach-o/dyld.h>
#include <string.h>
#include <sys/wait.h>
#include <signal.h>

static pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

int main() {
    // launchd/GigakuSound.app/Contents/MacOS/GigakuSound sits five levels below the repo
    // root — derive it instead of hardcoding, so moving the repo can't strand the agent.
    char raw[PATH_MAX], repo[PATH_MAX];
    uint32_t size = sizeof(raw);
    if (_NSGetExecutablePath(raw, &size) != 0 || !realpath(raw, repo)) return 1;
    for (int i = 0; i < 5; i++) {
        char *slash = strrchr(repo, '/');
        if (!slash) return 1;
        *slash = '\0';
    }
    if (chdir(repo) != 0) return 1;

    // fork, NOT exec: the parent must stay alive so it remains the child's
    // *responsible process*. TCC then judges this signed bundle — which holds the
    // Accessibility grant — instead of whatever python the venv points at. exec'ing
    // would replace this image and hand the decision to Homebrew's python, whose
    // path and cdhash change on every upgrade, silently killing the event tap.
    child = fork();
    if (child < 0) return 1;
    if (child == 0) {
        execv(".venv/bin/python3",
              (char*[]){".venv/bin/python3", "-u", "scripts/scroll_volume.py", NULL});
        return 1;
    }
    signal(SIGTERM, forward);
    signal(SIGINT, forward);

    int status = 0;
    while (waitpid(child, &status, 0) < 0 && errno == EINTR) continue;
    // Propagate the child's fate so launchd's KeepAlive respawns as before.
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
