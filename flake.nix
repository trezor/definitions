{
  description = "Trezor definitions development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/59682e0069f0ed0a452e2179a7f4c1f247027b9e";
    # only used by the shell.nix compatibility shim
    flake-compat = {
      url = "github:edolstra/flake-compat";
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
        default = pkgs.mkShell {
          packages = [
            pkgs.bash
            pkgs.git
            pkgs.gnumake
            pkgs.python312
            pkgs.ruff
            pkgs.uv
          ] ++ pkgs.lib.optionals pkgs.stdenv.isDarwin [
            pkgs.libiconv
          ];

          # Wheels like cryptography may need these at runtime
          LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [
            pkgs.libffi
            pkgs.openssl
          ];
          DYLD_LIBRARY_PATH = "${pkgs.libffi}/lib:${pkgs.openssl.out}/lib";

          NIX_ENFORCE_PURITY = 0;

          # Fix bdist-wheel problem by setting source date epoch to a more recent date
          SOURCE_DATE_EPOCH = 1600000000;

          # don't try to use stack protector for Apple Silicon binaries
          # it's broken at the moment
          hardeningDisable = pkgs.lib.optionals (pkgs.stdenv.isDarwin && pkgs.stdenv.isAarch64) [ "stackprotector" ];

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
