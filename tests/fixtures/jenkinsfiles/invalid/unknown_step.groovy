// expect: line 7: unknown step 'archiveArtifacts'
pipeline {
    agent any
    stages {
        stage('A') {
            steps {
                archiveArtifacts 'build/**'
            }
        }
    }
}
