import os
import shlex
import sys
import tempfile
import warnings
from typing import Any

import click
from click import Command
from click.utils import safecall
from pip._internal.commands import create_command
from pip._internal.req.constructors import install_req_from_line
from pip._internal.utils.misc import redact_auth_from_url

from .._compat import parse_requirements
from ..cache import DependencyCache
from ..exceptions import PipToolsError
from ..locations import CACHE_DIR
from ..logging import log
from ..repositories import LocalRequirementsRepository, PyPIRepository
from ..resolver import Resolver
from ..utils import UNSAFE_PACKAGES, dedup, is_pinned_requirement, key_from_ireq
from ..writer import OutputWriter

DEFAULT_REQUIREMENTS_FILE = "requirements.in"
DEFAULT_REQUIREMENTS_OUTPUT_FILE = "requirements.txt"


def _get_default_option(option_name: str) -> Any:
    """
    Get default value of the pip's option (including option from pip.conf)
    by a given option name.
    """
    install_command = create_command("install")
    default_values = install_command.parser.get_default_values()
    return getattr(default_values, option_name)


class BaseCommand(Command):
    _os_args = None

    def parse_args(self, ctx, args):
        """
        Override base `parse_args` to store the argument part of `sys.argv`.
        """
        self._os_args = set(args)
        return super().parse_args(ctx, args)

    def has_arg(self, arg_name):
        """
        Detect whether a given arg name (including negative counterparts
        to the arg, e.g. --no-arg) is present in the argument part of `sys.argv`.
        """
        command_options = {option.name: option for option in self.params}
        option = command_options[arg_name]
        args = set(option.opts + option.secondary_opts)
        return bool(self._os_args & args)


@click.command(
    cls=BaseCommand, context_settings={"help_option_names": ("-h", "--help")}
)
@click.version_option()
@click.pass_context
@click.option("-v", "--verbose", count=True, help="Show more output")
@click.option("-q", "--quiet", count=True, help="Give less output")
@click.option(
    "-n",
    "--dry-run",
    is_flag=True,
    help="Only show what would happen, don't change anything",
)
@click.option(
    "-p",
    "--pre",
    is_flag=True,
    default=None,
    help="Allow resolving to prereleases (default is not)",
)
@click.option(
    "-r",
    "--rebuild",
    is_flag=True,
    help="Clear any caches upfront, rebuild from scratch",
)
@click.option(
    "-f",
    "--find-links",
    multiple=True,
    help="Look for archives in this directory or on this HTML page",
)
@click.option(
    "-i",
    "--index-url",
    help="Change index URL (defaults to {index_url})".format(
        index_url=redact_auth_from_url(_get_default_option("index_url"))
    ),
)
@click.option(
    "--extra-index-url", multiple=True, help="Add additional index URL to search"
)
@click.option("--cert", help="Path to alternate CA bundle.")
@click.option(
    "--client-cert",
    help="Path to SSL client certificate, a single file containing "
    "the private key and the certificate in PEM format.",
)
@click.option(
    "--trusted-host",
    multiple=True,
    help="Mark this host as trusted, even though it does not have "
    "valid or any HTTPS.",
)
@click.option(
    "--header/--no-header",
    is_flag=True,
    default=True,
    help="Add header to generated file",
)
@click.option(
    "--index/--no-index",
    is_flag=True,
    default=True,
    help="DEPRECATED: Add index URL to generated file",
)
@click.option(
    "--emit-trusted-host/--no-emit-trusted-host",
    is_flag=True,
    default=True,
    help="Add trusted host option to generated file",
)
@click.option(
    "--annotate/--no-annotate",
    is_flag=True,
    default=True,
    help="Annotate results, indicating where dependencies come from",
)
@click.option(
    "-U",
    "--upgrade",
    is_flag=True,
    default=False,
    help="Try to upgrade all dependencies to their latest versions",
)
@click.option(
    "-P",
    "--upgrade-package",
    "upgrade_packages",
    nargs=1,
    multiple=True,
    help="Specify particular packages to upgrade.",
)
@click.option(
    "-o",
    "--output-file",
    nargs=1,
    default=None,
    type=click.File("w+b", atomic=True, lazy=True),
    help=(
        "Output file name. Required if more than one input file is given. "
        "Will be derived from input file otherwise."
    ),
)
@click.option(
    "--allow-unsafe/--no-allow-unsafe",
    is_flag=True,
    default=False,
    help=(
        "Pin packages considered unsafe: {}.\n\n"
        "WARNING: Future versions of pip-tools will enable this behavior by default. "
        "Use --no-allow-unsafe to keep the old behavior. It is recommended to pass the "
        "--allow-unsafe now to adapt to the upcoming change.".format(
            ", ".join(sorted(UNSAFE_PACKAGES))
        )
    ),
)
@click.option(
    "--generate-hashes",
    is_flag=True,
    default=False,
    help="Generate pip 8 style hashes in the resulting requirements file.",
)
@click.option(
    "--reuse-hashes/--no-reuse-hashes",
    is_flag=True,
    default=True,
    help=(
        "Improve the speed of --generate-hashes by reusing the hashes from an "
        "existing output file."
    ),
)
@click.option(
    "--max-rounds",
    default=10,
    help="Maximum number of rounds before resolving the requirements aborts.",
)
@click.argument("src_files", nargs=-1, type=click.Path(exists=True, allow_dash=True))
@click.option(
    "--build-isolation/--no-build-isolation",
    is_flag=True,
    default=True,
    help="Enable isolation when building a modern source distribution. "
    "Build dependencies specified by PEP 518 must be already installed "
    "if build isolation is disabled.",
)
@click.option(
    "--emit-find-links/--no-emit-find-links",
    is_flag=True,
    default=True,
    help="Add the find-links option to generated file",
)
@click.option(
    "--cache-dir",
    help="Store the cache data in DIRECTORY.",
    default=CACHE_DIR,
    show_default=True,
    type=click.Path(file_okay=False, writable=True),
)
@click.option("--pip-args", help="Arguments to pass directly to the pip command.")
@click.option(
    "--emit-index-url/--no-emit-index-url",
    is_flag=True,
    default=True,
    help="Add index URL to generated file",
)
def cli(
    ctx,
    verbose,
    quiet,
    dry_run,
    pre,
    rebuild,
    find_links,
    index_url,
    extra_index_url,
    cert,
    client_cert,
    trusted_host,
    header,
    index,
    emit_trusted_host,
    annotate,
    upgrade,
    upgrade_packages,
    output_file,
    allow_unsafe,
    generate_hashes,
    reuse_hashes,
    src_files,
    max_rounds,
    build_isolation,
    emit_find_links,
    cache_dir,
    pip_args,
    emit_index_url,
):
    """Compiles requirements.txt from requirements.in specs."""
    log.verbosity = verbose - quiet

    if len(src_files) == 0:
        if os.path.exists(DEFAULT_REQUIREMENTS_FILE):
            src_files = (DEFAULT_REQUIREMENTS_FILE,)
        elif os.path.exists("setup.py"):
            src_files = ("setup.py",)
        else:
            raise click.BadParameter(
                (
                    "If you do not specify an input file, "
                    "the default is {} or setup.py"
                ).format(DEFAULT_REQUIREMENTS_FILE)
            )

    if not output_file:
        # An output file must be provided for stdin
        if src_files == ("-",):
            raise click.BadParameter("--output-file is required if input is from stdin")
        # Use default requirements output file if there is a setup.py the source file
        elif os.path.basename(src_files[0]) == "setup.py":
            file_name = os.path.join(
                os.path.dirname(src_files[0]), DEFAULT_REQUIREMENTS_OUTPUT_FILE
            )
        # An output file must be provided if there are multiple source files
        elif len(src_files) > 1:
            raise click.BadParameter(
                "--output-file is required if two or more input files are given."
            )
        # Otherwise derive the output file from the source file
        else:
            base_name = src_files[0].rsplit(".", 1)[0]
            file_name = base_name + ".txt"

        output_file = click.open_file(file_name, "w+b", atomic=True, lazy=True)

        # Close the file at the end of the context execution
        ctx.call_on_close(safecall(output_file.close_intelligently))

    if cli.has_arg("index") and cli.has_arg("emit_index_url"):
        raise click.BadParameter(
            "--index/--no-index and --emit-index-url/--no-emit-index-url "
            "are mutually exclusive."
        )
    elif cli.has_arg("index"):
        warnings.warn(
            "--index and --no-index are deprecated and will be removed "
            "in future versions. Use --emit-index-url/--no-emit-index-url instead.",
            category=FutureWarning,
        )
        emit_index_url = index

    ###
    # Setup
    ###

    right_args = shlex.split(pip_args or "")
    pip_args = []
    for link in find_links:
        pip_args.extend(["-f", link])
    if index_url:
        pip_args.extend(["-i", index_url])
    for extra_index in extra_index_url:
        pip_args.extend(["--extra-index-url", extra_index])
    if cert:
        pip_args.extend(["--cert", cert])
    if client_cert:
        pip_args.extend(["--client-cert", client_cert])
    if pre:
        pip_args.extend(["--pre"])
    for host in trusted_host:
        pip_args.extend(["--trusted-host", host])

    if not build_isolation:
        pip_args.append("--no-build-isolation")
    pip_args.extend(right_args)

    repository = PyPIRepository(pip_args, cache_dir=cache_dir)

    # Parse all constraints coming from --upgrade-package/-P
    upgrade_reqs_gen = (install_req_from_line(pkg) for pkg in upgrade_packages)
    upgrade_install_reqs = {
        key_from_ireq(install_req): install_req for install_req in upgrade_reqs_gen
    }

    existing_pins_to_upgrade = set()

    # Proxy with a LocalRequirementsRepository if --upgrade is not specified
    # (= default invocation)
    if not upgrade and os.path.exists(output_file.name):
        # Use a temporary repository to ensure outdated(removed) options from
        # existing requirements.txt wouldn't get into the current repository.
        tmp_repository = PyPIRepository(pip_args, cache_dir=cache_dir)
        ireqs = parse_requirements(
            output_file.name,
            finder=tmp_repository.finder,
            session=tmp_repository.session,
            options=tmp_repository.options,
        )

        # Exclude packages from --upgrade-package/-P from the existing
        # constraints, and separately gather pins to be upgraded
        existing_pins = {}
        for ireq in filter(is_pinned_requirement, ireqs):
            key = key_from_ireq(ireq)
            if key in upgrade_install_reqs:
                existing_pins_to_upgrade.add(key)
            else:
                existing_pins[key] = ireq
        repository = LocalRequirementsRepository(
            existing_pins, repository, reuse_hashes=reuse_hashes
        )

    ###
    # Parsing/collecting initial requirements
    ###

    constraints = []
    for src_file in src_files:
        is_setup_file = os.path.basename(src_file) == "setup.py"
        if is_setup_file or src_file == "-":
            # pip requires filenames and not files. Since we want to support
            # piping from stdin, we need to briefly save the input from stdin
            # to a temporary file and have pip read that.  also used for
            # reading requirements from install_requires in setup.py.
            tmpfile = tempfile.NamedTemporaryFile(mode="wt", delete=False)
            if is_setup_file:
                from distutils.core import run_setup

                dist = run_setup(src_file)
                tmpfile.write("\n".join(dist.install_requires))
                comes_from = f"{dist.get_name()} ({src_file})"
            else:
                tmpfile.write(sys.stdin.read())
                comes_from = "-r -"
            tmpfile.flush()
            reqs = list(
                parse_requirements(
                    tmpfile.name,
                    finder=repository.finder,
                    session=repository.session,
                    options=repository.options,
                )
            )
            for req in reqs:
                req.comes_from = comes_from
            constraints.extend(reqs)
        else:
            constraints.extend(
                parse_requirements(
                    src_file,
                    finder=repository.finder,
                    session=repository.session,
                    options=repository.options,
                )
            )

    primary_packages = {
        key_from_ireq(ireq) for ireq in constraints if not ireq.constraint
    }

    allowed_upgrades = primary_packages | existing_pins_to_upgrade
    constraints.extend(
        ireq for key, ireq in upgrade_install_reqs.items() if key in allowed_upgrades
    )

    # Filter out pip environment markers which do not match (PEP496)
    constraints = [
        req for req in constraints if req.markers is None or req.markers.evaluate()
    ]

    log.debug("Using indexes:")
    with log.indentation():
        for index_url in dedup(repository.finder.index_urls):
            log.debug(redact_auth_from_url(index_url))

    if repository.finder.find_links:
        log.debug("")
        log.debug("Using links:")
        with log.indentation():
            for find_link in dedup(repository.finder.find_links):
                log.debug(redact_auth_from_url(find_link))

    try:
        resolver = Resolver(
            constraints,
            repository,
            prereleases=repository.finder.allow_all_prereleases or pre,
            cache=DependencyCache(cache_dir),
            clear_caches=rebuild,
            allow_unsafe=allow_unsafe,
        )
        results = resolver.resolve(max_rounds=max_rounds)
        if generate_hashes:
            hashes = resolver.resolve_hashes(results)
        else:
            hashes = None
    except PipToolsError as e:
        log.error(str(e))
        sys.exit(2)

    log.debug("")

    ##
    # Output
    ##

    writer = OutputWriter(
        output_file,
        click_ctx=ctx,
        dry_run=dry_run,
        emit_header=header,
        emit_index_url=emit_index_url,
        emit_trusted_host=emit_trusted_host,
        annotate=annotate,
        generate_hashes=generate_hashes,
        default_index_url=repository.DEFAULT_INDEX_URL,
        index_urls=repository.finder.index_urls,
        trusted_hosts=repository.finder.trusted_hosts,
        format_control=repository.finder.format_control,
        allow_unsafe=allow_unsafe,
        find_links=repository.finder.find_links,
        emit_find_links=emit_find_links,
    )
    writer.write(
        results=results,
        unsafe_requirements=resolver.unsafe_constraints,
        markers={
            key_from_ireq(ireq): ireq.markers for ireq in constraints if ireq.markers
        },
        hashes=hashes,
    )

    if dry_run:
        log.info("Dry-run, so nothing updated.")
