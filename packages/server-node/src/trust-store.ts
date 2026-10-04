// Validate administrator CA chains and signed offline CRLs before a peer session.
import { webcrypto, X509Certificate } from "node:crypto";
import { closeSync, existsSync, openSync, readSync, readdirSync } from "node:fs";
import { isIP } from "node:net";
import { join } from "node:path";
import { InMemoryCertificateStore } from "node-opcua-common";
import { exploreCertificate, split_der } from "node-opcua-crypto";
import { StatusCodes } from "node-opcua-status-code";
import {
  BasicConstraints,
  Certificate,
  CertificateChainValidationEngine,
  CertificateRevocationList,
  ChainValidationCode,
  CryptoEngine,
  ExtKeyUsage,
} from "pkijs";

const cryptoEngine = new CryptoEngine({ name: "node", crypto: webcrypto as unknown as Crypto });
const REMEDIATION =
  "Check the configured CA chain, current signed CRLs, endpoint hostname and " +
  "OPCUA_SERVER_APPLICATION_URI; update the trust store before reconnecting.";
export const trustRefusal = (status: string) =>
  `OPCUA_SERVER_TRUST_STORE: ${status}. ${REMEDIATION}`;

function material(root: string): Record<string, Buffer[]> {
  const result: Record<string, Buffer[]> = {};
  let count = 0;
  let total = 0;
  for (const folder of ["trusted/certs", "issuers/certs", "trusted/crl", "issuers/crl"]) {
    const directory = join(root, folder);
    result[folder] = [];
    if (!existsSync(directory)) continue;
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      if (!entry.isFile()) continue;
      if (++count > 100) throw new Error("invalid bounded trust material");
      const handle = openSync(join(directory, entry.name), "r");
      const buffer = Buffer.alloc(1024 * 1024 + 1);
      let size = 0;
      try {
        while (size < buffer.length) {
          const read = readSync(handle, buffer, size, buffer.length - size, null);
          if (!read) break;
          size += read;
        }
      } finally {
        closeSync(handle);
      }
      total += size;
      if (size > 1024 * 1024 || total > 16 * 1024 * 1024) {
        throw new Error("invalid bounded trust material");
      }
      result[folder].push(buffer.subarray(0, size));
    }
  }
  return result;
}

function crl(data: Buffer): CertificateRevocationList {
  const raw = data.toString("ascii").startsWith("-----BEGIN")
    ? Buffer.from(
        data.toString("ascii").replace(/-----BEGIN X509 CRL-----|-----END X509 CRL-----|\s/g, ""),
        "base64"
      )
    : data;
  return CertificateRevocationList.fromBER(Uint8Array.from(raw).buffer);
}

// Critical extensions outside this supported profile must never be silently ignored.
const criticalExtensions = new Set([
  "2.5.29.19",
  "2.5.29.15",
  "2.5.29.17",
  "2.5.29.30",
  "2.5.29.37",
]);
function unsupportedCritical(certificate: Certificate): boolean {
  return (certificate.extensions ?? []).some(
    (extension) => extension.critical && !criticalExtensions.has(extension.extnID)
  );
}

function pathLengthProblem(path: Certificate[]): boolean {
  // PKIJS returns the leaf first. Count non-self-issued intermediate CAs below each issuer.
  for (let index = 1; index < path.length; index++) {
    const constraints = path[index].extensions?.find(
      (extension) => extension.extnID === "2.5.29.19"
    )?.parsedValue as BasicConstraints | undefined;
    if (constraints?.pathLenConstraint === undefined) continue;
    const limit = constraints.pathLenConstraint;
    if (typeof limit !== "number" || limit < 0) return true;
    const intermediates = path.slice(1, index).filter((cert) => !cert.subject.isEqual(cert.issuer));
    if (intermediates.length > limit) return true;
  }
  return false;
}

export interface TrustIdentity {
  applicationUri: string;
  endpoint: string;
  advertisedUri: string;
}

function identityProblem(cert: Buffer, identity: TrustIdentity): string | null {
  const names = exploreCertificate(cert).tbsCertificate.extensions?.subjectAltName;
  if (
    identity.advertisedUri !== identity.applicationUri ||
    !names?.uniformResourceIdentifier?.includes(identity.applicationUri)
  ) {
    return "BadCertificateUriInvalid";
  }
  const hostname = new URL(identity.endpoint).hostname.replace(/^\[|\]$/g, "");
  const x509 = new X509Certificate(cert);
  const match = isIP(hostname)
    ? x509.checkIP(hostname)
    : x509.checkHost(hostname, { subject: "never", wildcards: false });
  return match ? null : "BadCertificateHostNameInvalid";
}

export async function certificateProblem(
  path: string,
  certificate: Buffer | Buffer[],
  identity?: TrustIdentity,
  now = new Date()
): Promise<string | null> {
  try {
    const loaded = material(path);
    const anchors = loaded["trusted/certs"].map((data) => new X509Certificate(data).raw);
    const issuers = loaded["issuers/certs"].map((data) => new X509Certificate(data).raw);
    if (!anchors.length) return "BadCertificateUntrusted";
    if (
      anchors.some((data) => {
        const anchor = new X509Certificate(data);
        return anchor.subject !== anchor.issuer || !anchor.verify(anchor.publicKey);
      })
    )
      return "BadCertificateInvalid";
    const authorities = [...anchors, ...issuers];
    if (authorities.some((data) => !new X509Certificate(data).ca)) {
      return "BadCertificateInvalid";
    }
    const peer = Array.isArray(certificate) ? certificate[0] : split_der(certificate)[0];
    if (!peer || peer.length > 1024 * 1024) return "BadCertificateInvalid";
    const leaf = Certificate.fromBER(Uint8Array.from(peer).buffer);
    const usage = leaf.extensions?.find((ext) => ext.extnID === "2.5.29.37")?.parsedValue as
      ExtKeyUsage | undefined;
    if (
      usage &&
      !usage.keyPurposes.includes("1.3.6.1.5.5.7.3.1") &&
      !usage.keyPurposes.includes("2.5.29.37.0")
    ) {
      return "BadCertificateInvalid";
    }
    const cas = authorities.map((data) => Certificate.fromBER(data));
    if ([leaf, ...cas].some(unsupportedCritical)) return "BadCertificateInvalid";
    const crls = [...loaded["trusted/crl"], ...loaded["issuers/crl"]].map(crl);
    if (
      !crls.length ||
      crls.some(
        (list) => !list.nextUpdate || list.thisUpdate.value > now || list.nextUpdate.value <= now
      )
    ) {
      return "BadCertificateRevocationUnknown";
    }
    if (
      crls.some((list) => list.crlExtensions?.extensions.some((extension) => extension.critical))
    ) {
      return "BadCertificateInvalid";
    }
    for (const list of crls) {
      let valid = false;
      for (let index = 0; index < cas.length; index++) {
        if (
          list.issuer.isEqual(cas[index].subject) &&
          exploreCertificate(authorities[index]).tbsCertificate.extensions?.keyUsage?.cRLSign &&
          (await list.verify({ issuerCertificate: cas[index] }, cryptoEngine))
        )
          valid = true;
      }
      if (!valid) return "BadCertificateInvalid";
    }
    if (leaf.notBefore.value > now || leaf.notAfter.value <= now) {
      return "BadCertificateTimeInvalid";
    }
    if (cas.some((ca) => ca.notBefore.value > now || ca.notAfter.value <= now)) {
      return "BadCertificateIssuerTimeInvalid";
    }
    if (crls.some((list) => list.isCertificateRevoked(leaf))) return "BadCertificateRevoked";
    if (cas.some((ca) => crls.some((list) => list.isCertificateRevoked(ca)))) {
      return "BadCertificateIssuerRevoked";
    }
    const engine = new CertificateChainValidationEngine({
      trustedCerts: anchors.map((data) => Certificate.fromBER(data)),
      certs: [...cas, leaf],
      crls,
      checkDate: now,
    });
    const result = await engine.verify({ passedWhenNotRevValues: false }, cryptoEngine);
    if (!result.result) {
      if (result.resultCode === ChainValidationCode.noRevocation) {
        return "BadCertificateRevocationUnknown";
      }
      if (
        [ChainValidationCode.noPath, ChainValidationCode.noValidPath].includes(result.resultCode) ||
        (result.resultCode === ChainValidationCode.unknown &&
          result.resultMessage === "No valid certificate paths found")
      ) {
        return "BadCertificateUntrusted";
      }
      return "BadCertificateInvalid";
    }
    if (!result.certificatePath || result.certificatePath.length > 8) {
      return "BadCertificateChainIncomplete";
    }
    if (pathLengthProblem(result.certificatePath)) return "BadCertificateInvalid";
    return identity ? identityProblem(peer, identity) : null;
  } catch {
    return "BadCertificateInvalid";
  }
}

/** Reload read-only administrator material on every native handshake check. */
export class TrustStore extends InMemoryCertificateStore {
  constructor(
    private readonly path: string,
    private readonly identity: TrustIdentity
  ) {
    super({ autoAcceptUnknown: false });
  }

  override async verifyCertificate(certificate: Buffer | Buffer[]): Promise<string> {
    return (await certificateProblem(this.path, certificate)) ?? "Good";
  }

  override async checkCertificate(certificate: Buffer | Buffer[]) {
    const problem = await certificateProblem(this.path, certificate, this.identity);
    if (problem) throw new Error(trustRefusal(problem));
    return StatusCodes.Good;
  }
}
