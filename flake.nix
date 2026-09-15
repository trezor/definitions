{
  description = "Trezor definitions development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    # only used by the shell.nix compatibility shim
    flake-compat = {
      url = "github:NixOS/flake-compat";
      flake = false;
    };
  };

  outputs =
    { nixpkgs, ... }:
    let
      forAllSystems =
        f:
        nixpkgs.lib.genAttrs
          [
            "x86_64-linux"
            "aarch64-linux"
            "aarch64-darwin"
          ]
          (system: f (import nixpkgs { inherit system; }));
    in
    {
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell rec {
          packages = with pkgs; [
            bash
            git
            gnumake
            python312
            ruff
            uv
          ] ++ lib.optionals stdenv.hostPlatform.isDarwin [
            libiconv
          ];

          # Wheels like cryptography may need these at runtime
          LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [
            pkgs.libffi
            pkgs.openssl
          ];
          DYLD_LIBRARY_PATH = LD_LIBRARY_PATH;

          # Fix bdist-wheel problem by setting source date epoch to a more recent date
          SOURCE_DATE_EPOCH = 1600000000;

          # Force uv to use the nix-provided Python instead of its own managed
          # builds. Without this, uv defaults to python-preference=managed +
          # python-downloads=automatic, silently downloading/reusing its own
          # interpreter and ignoring python3 on PATH.
          UV_PYTHON_PREFERENCE = "only-system";
          UV_PYTHON_DOWNLOADS = "never";
        };
      });
    };
}
