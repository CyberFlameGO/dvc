import errno
import logging
import os
import platform

logger = logging.getLogger(__name__)


class System:
    @staticmethod
    def hardlink(source, link_name):
        os.link(source, link_name)

    @staticmethod
    def symlink(source, link_name):
        os.symlink(source, link_name)

    @staticmethod
    def _reflink_darwin(src, dst):
        import ctypes

        def _cdll(name):
            return ctypes.CDLL(name, use_errno=True)

        LIBC = "libc.dylib"
        LIBC_FALLBACK = "/usr/lib/libSystem.dylib"
        try:
            clib = _cdll(LIBC)
        except OSError as exc:
            logger.debug(
                "unable to access '{}' (errno '{}'). "
                "Falling back to '{}'.".format(LIBC, exc.errno, LIBC_FALLBACK)
            )
            if exc.errno != errno.ENOENT:
                raise
            # NOTE: trying to bypass System Integrity Protection (SIP)
            clib = _cdll(LIBC_FALLBACK)

        if not hasattr(clib, "clonefile"):
            raise OSError(
                errno.ENOTSUP,
                "'clonefile' not supported by the standard library",
            )

        clonefile = clib.clonefile
        clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        clonefile.restype = ctypes.c_int

        ret = clonefile(
            ctypes.c_char_p(os.fsencode(src)),
            ctypes.c_char_p(os.fsencode(dst)),
            ctypes.c_int(0),
        )
        if ret:
            err = ctypes.get_errno()
            msg = os.strerror(err)
            raise OSError(err, msg)

    @staticmethod
    def _reflink_linux(src, dst):
        import fcntl  # pylint: disable=import-error

        from funcy import suppress

        FICLONE = 0x40049409

        try:
            with open(src, "rb") as s, open(dst, "wb+") as d:
                fcntl.ioctl(d.fileno(), FICLONE, s.fileno())
        except OSError:
            with suppress(OSError):
                os.unlink(dst)
            raise

    @staticmethod
    def reflink(source, link_name):
        from dvc.utils.fs import umask

        source, link_name = os.fspath(source), os.fspath(link_name)

        system = platform.system()
        if system == "Darwin":
            System._reflink_darwin(source, link_name)
        elif system == "Linux":
            System._reflink_linux(source, link_name)
        else:
            raise OSError(
                errno.ENOTSUP,
                f"reflink is not supported on '{system}'",
            )

        # NOTE: reflink has a new inode, but has the same mode as the src,
        # so we need to chmod it to look like a normal copy.
        os.chmod(link_name, 0o666 & ~umask)

    @staticmethod
    def inode(path):
        return os.lstat(path).st_ino

    @staticmethod
    def is_symlink(path):
        return os.path.islink(path)

    @staticmethod
    def is_hardlink(path):
        return os.stat(path).st_nlink > 1
