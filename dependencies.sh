#!/bin/bash

# You may need C libraries for some functionality. PIP won't be much help, for instance, without JPEG and PNG support.

if which -s pip3; then
    PIP=`which pip3`
elif which -s pip; then
    PIP=`which pip`
else
    echo pip not found, aborting.
    exit 1
fi
echo Using "$PIP"
PIP_VERSION=`"$PIP" -V`
if ! [[ $PIP_VERSION =~ python\ 3 ]]; then
    echo "Pip is for wrong python: $PIP_VERSION"
    exit 1
fi

"$PIP" install lxml
"$PIP" install beautifulsoup || exit
"$PIP" install filemagic
"$PIP" install rjsmin
"$PIP" install csscompressor
"$PIP" install pillow
