{ config, lib, pkgs, ... }:

with lib;

let
  cfg = config.services.syncplay;

  cmdArgs =
    [ "--port" cfg.port ]
    ++ optionals (cfg.salt != null) [ "--salt" cfg.salt ]
    ++ optionals (cfg.certDir != null) [ "--tls" cfg.certDir ]
    ++ cfg.extraArgs;

in
{
  options = {
    services.syncplay = {
      enable = mkOption {
        type = types.bool;
        default = false;
        description = "If enabled, start the Syncplay server.";
      };

      port = mkOption {
        type = types.port;
        default = 8999;
        description = ''
          TCP port to bind to.
        '';
      };

      salt = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = ''
          Salt to allow room operator passwords generated by this server
          instance to still work when the server is restarted.  The salt will be
          readable in the nix store and the processlist.  If this is not
          intended use `saltFile` instead.  Mutually exclusive with
          <option>services.syncplay.saltFile</option>.
        '';
      };

      saltFile = mkOption {
        type = types.nullOr types.path;
        default = null;
        description = ''
          Path to the file that contains the server salt.  This allows room
          operator passwords generated by this server instance to still work
          when the server is restarted.  `null`, the server doesn't load the
          salt from a file.  Mutually exclusive with
          <option>services.syncplay.salt</option>.
        '';
      };

      certDir = mkOption {
        type = types.nullOr types.path;
        default = null;
        description = ''
          TLS certificates directory to use for encryption. See
          <https://github.com/Syncplay/syncplay/wiki/TLS-support>.
        '';
      };

      extraArgs = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = ''
          Additional arguments to be passed to the service.
        '';
      };

      user = mkOption {
        type = types.str;
        default = "nobody";
        description = ''
          User to use when running Syncplay.
        '';
      };

      group = mkOption {
        type = types.str;
        default = "nogroup";
        description = ''
          Group to use when running Syncplay.
        '';
      };

      passwordFile = mkOption {
        type = types.nullOr types.path;
        default = null;
        description = ''
          Path to the file that contains the server password. If
          `null`, the server doesn't require a password.
        '';
      };
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.salt == null || cfg.saltFile == null;
        message = "services.syncplay.salt and services.syncplay.saltFile are mutually exclusive.";
      }
    ];
    systemd.services.syncplay = {
      description = "Syncplay Service";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];

      serviceConfig = {
        User = cfg.user;
        Group = cfg.group;
        LoadCredential = lib.optional (cfg.passwordFile != null) "password:${cfg.passwordFile}"
          ++ lib.optional (cfg.saltFile != null) "salt:${cfg.saltFile}";
      };

      script = ''
        ${lib.optionalString (cfg.passwordFile != null) ''
          export SYNCPLAY_PASSWORD=$(cat "''${CREDENTIALS_DIRECTORY}/password")
        ''}
        ${lib.optionalString (cfg.saltFile != null) ''
          export SYNCPLAY_SALT=$(cat "''${CREDENTIALS_DIRECTORY}/salt")
        ''}
        exec ${pkgs.syncplay-nogui}/bin/syncplay-server ${escapeShellArgs cmdArgs}
      '';
    };
  };
}
