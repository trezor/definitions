# Compatibility shim for `nix-shell` users without flakes enabled.
# Loads the default devShell from flake.nix via flake-compat
# (version pinned in flake.lock).
(import (fetchTarball {
  url =
    let
      lock = builtins.fromJSON (builtins.readFile ./flake.lock);
      node = lock.nodes.${lock.nodes.root.inputs.flake-compat}.locked;
    in
    "https://github.com/${node.owner}/${node.repo}/archive/${node.rev}.tar.gz";
  sha256 =
    let
      lock = builtins.fromJSON (builtins.readFile ./flake.lock);
      node = lock.nodes.${lock.nodes.root.inputs.flake-compat}.locked;
    in
    node.narHash;
}) { src = ./.; }).shellNix
