<?php
namespace Shiftcodes\Workloads;
final class PackageIdentity
{
 public static function digest(): string {
  $root=dirname(__DIR__);$files=['composer.json'=>hash_file('sha256',$root.'/composer.json')];
  foreach(['src','config','database'] as $directory){
   $iterator=new \RecursiveIteratorIterator(new \RecursiveDirectoryIterator($root.'/'.$directory,\FilesystemIterator::SKIP_DOTS));
   foreach($iterator as $file){if($file->isLink())throw new ProtocolError('instrumentation_changed');if($file->isFile())$files[substr($file->getPathname(),strlen($root)+1)]=hash_file('sha256',$file->getPathname());}
  }
  return hash('sha256',Protocol::canonical($files));
 }
}
