#!/bin/bash
if [ -z ${APPNAME} ] || [ -z ${ENVNAME} ];then
   echo "Environment Variable APPNAME or ENVNAME is not set"
   exit 1
else
  [ -f tfmeta.tf ] && rm tfmeta.tf
  cat tfmeta.templ | sed "s%<appname>/<envname>%${APPNAME}/${ENVNAME}%" > tfmeta.tf
fi
