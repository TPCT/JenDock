// expect: line 3: '-v' is not allowed
pipeline {
    agent { docker { image 'node:22'; args '-v /:/host' } }
    stages { stage('A') { steps { sh 'id' } } }
}
