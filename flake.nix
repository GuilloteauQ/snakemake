{
  description = "A very basic flake";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
    in
    {

     packages.${system} = {
        snakemake = pkgs.snakemake.overrideAttrs (finalAttrs: previousAttrs: {
          src = ./.;
          pname = previousAttrs.pname + "-nix";
        });

     };

      devShells.${system} = {
        default = pkgs.mkShell {
          buildInputs = pkgs.snakemake.buildInputs ++ pkgs.snakemake.propagatedBuildInputs;
        };
      };

    };
}
